"""Signed payment events: a small, messy demo scenario, or a steady stream for load tests.

TAILHOOK_SECRET=... uv run python -m app.simulator mixed --url https://.../webhooks
TAILHOOK_SECRET=... uv run python -m app.simulator load --url https://.../webhooks --rate 1000
uv run python -m app.simulator verify --run <run id> --log-group /aws/lambda/tailhook-worker
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import string
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from app import signing
from app.declines import DECLINE_CODES
from app.store import iso, utcnow

CARDS = [("visa", "4242"), ("mastercard", "4444"), ("amex", "0005"), ("discover", "1117")]
ALPHABET = string.ascii_letters + string.digits
# ponytail: tops out near MAX_IN_FLIGHT / latency (about 2,000/s at 100 ms); run more generators
# to go higher.
MAX_IN_FLIGHT = 200


@dataclass(frozen=True)
class Delivery:
    msg_id: str
    body: str


def event_body(
    event_type: str,
    payment_id: str,
    created_at: datetime,
    *,
    amount: int = 4200,
    card: tuple[str, str] = CARDS[0],
    attempt: int = 1,
    code: str | None = None,
    note: str | None = None,
    run: str | None = None,
) -> str:
    data = {
        "payment_id": payment_id,
        "amount": amount,
        "currency": "USD",
        "card": {"brand": card[0], "last4": card[1]},
        "attempt": attempt,
    }
    if code:
        reason = DECLINE_CODES.get(code, {}).get("reason", "unknown")
        data["decline"] = {"code": code, "reason": reason}
    if note:
        data["note"] = note
    if run:
        data["run"] = run
    return json.dumps({"type": event_type, "timestamp": iso(created_at), "data": data})


def mixed(seed: int, start: datetime) -> list[Delivery]:
    """About 20 events across 14 payments, sent with the mess real senders produce: a refund
    that arrives before the payment it refunds, and three events delivered twice."""
    rng = random.Random(seed)
    kinds = ["paid"] * 6 + ["declined"] * 3 + ["declined_then_paid"] * 2
    kinds += ["refunded"] * 2 + ["disputed"]
    rng.shuffle(kinds)
    clock = start
    sends: list[Delivery] = []

    def emit(event_type: str, payment_id: str, **fields) -> Delivery:
        nonlocal clock
        clock += timedelta(seconds=rng.randint(2, 30))
        msg_id = "msg_" + "".join(rng.choices(ALPHABET, k=20))
        delivery = Delivery(msg_id, event_body(event_type, payment_id, clock, **fields))
        sends.append(delivery)
        return delivery

    overtaken = False
    for kind in kinds:
        payment_id = "pay_" + "".join(rng.choices(ALPHABET, k=8))
        fields = {"amount": rng.randrange(500, 25_000, 100), "card": rng.choice(CARDS)}
        if kind.startswith("declined"):
            emit("payment.failed", payment_id, code=rng.choice(list(DECLINE_CODES)), **fields)
            if kind == "declined_then_paid":
                emit("payment.succeeded", payment_id, attempt=2, **fields)
            continue
        paid = emit("payment.succeeded", payment_id, **fields)
        if kind == "refunded":
            refund = emit("refund.created", payment_id, **fields)
            if not overtaken:
                sends.remove(paid)
                sends.insert(sends.index(refund) + 1, paid)
                overtaken = True
        elif kind == "disputed":
            emit("dispute.opened", payment_id, **fields)

    for original in rng.sample(sends, 3):  # the sender retries a few deliveries
        sends.insert(rng.randrange(sends.index(original) + 1, len(sends) + 1), original)
    return sends


def send(url: str, secret: str, deliveries: list[Delivery]) -> Counter:
    """POST each delivery in order and count the response codes."""
    results = Counter()
    for delivery in deliveries:
        request = urllib.request.Request(
            url,
            data=delivery.body.encode(),
            headers=signing.signed_headers(secret, delivery.msg_id, delivery.body),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                results[response.status] += 1
        except urllib.error.HTTPError as error:
            results[error.code] += 1
    return results


async def load(url: str, secret: str, rate: int, duration: int, run: str) -> dict:
    """Send rate * duration unique events at a steady rate and time each response."""
    import httpx  # dev dependency, only needed here

    total = rate * duration
    latencies: list[float] = []
    statuses: Counter = Counter()
    limits = httpx.Limits(max_connections=MAX_IN_FLIGHT, max_keepalive_connections=MAX_IN_FLIGHT)
    # Requests wait here rather than in httpx's pool, which rescans its whole queue every time
    # a request finishes.
    gate = asyncio.Semaphore(MAX_IN_FLIGHT)

    async with httpx.AsyncClient(limits=limits, timeout=10) as client:

        async def one(n: int) -> None:
            msg_id = f"msg_{run}_{n}"
            body = event_body("payment.succeeded", f"pay_{run}_{n}", utcnow(), run=run)
            async with gate:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        url, content=body, headers=signing.signed_headers(secret, msg_id, body)
                    )
                    statuses[response.status_code] += 1
                except httpx.HTTPError as error:
                    statuses[type(error).__name__] += 1
                latencies.append(time.perf_counter() - started)

        began = time.perf_counter()
        tasks = []
        for n in range(total):
            delay = began + n / rate - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(n)))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - began

    return {
        "run": run,
        "sent": total,
        "seconds": round(elapsed, 1),
        "rate_per_second": round(total / elapsed),
        "statuses": {str(k): v for k, v in statuses.items()},
        "p50_ms": round(statistics.median(latencies) * 1000),
        "p99_ms": round(statistics.quantiles(latencies, n=100)[98] * 1000),
    }


def verify(run: str, log_group: str) -> dict:
    """Count the worker's outcomes for one load-test run with CloudWatch Logs Insights."""
    import boto3

    logs = boto3.client("logs")
    now = int(time.time())
    query_id = logs.start_query(
        logGroupName=log_group,
        startTime=now - 6 * 3600,
        endTime=now,
        queryString=f'filter run = "{run}" | stats count(*) as n by outcome',
    )["queryId"]
    while (result := logs.get_query_results(queryId=query_id))["status"] in (
        "Scheduled",
        "Running",
    ):
        time.sleep(1)
    rows = [{field["field"]: field["value"] for field in row} for row in result["results"]]
    return {row["outcome"]: int(row["n"]) for row in rows}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("mixed", help="send the demo scenario once")
    one.add_argument("--url", required=True)
    one.add_argument("--seed", type=int)
    many = commands.add_parser("load", help="send a steady stream of unique events")
    many.add_argument("--url", required=True)
    many.add_argument("--rate", type=int, default=1000)
    many.add_argument("--duration", type=int, default=60)
    check = commands.add_parser("verify", help="count what the worker did with a load run")
    check.add_argument("--run", required=True)
    check.add_argument("--log-group", default="/aws/lambda/tailhook-worker")
    args = parser.parse_args()

    if args.command == "verify":
        if not args.run.isalnum():
            print("--run should be the hex id printed by the load command", file=sys.stderr)
            return 2
        print(json.dumps(verify(args.run, args.log_group)))
        return 0

    secret = os.environ.get("TAILHOOK_SECRET")
    if not secret:
        print("set TAILHOOK_SECRET to the inbound signing secret", file=sys.stderr)
        return 2
    if args.command == "mixed":
        seed = args.seed if args.seed is not None else random.randrange(1_000_000)
        print(json.dumps(dict(send(args.url, secret, mixed(seed, utcnow())))))
    else:
        result = asyncio.run(load(args.url, secret, args.rate, args.duration, os.urandom(4).hex()))
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
