"""Grade the agent against evals/cases.json.

    uv run python -m evals.run --model haiku
    uv run python -m evals.run --model opus --only refund-not-received

Each case's events go into a local moto server (DynamoDB only) through the real worker code,
with a stand-in merchant app that fails refunds. Only the model calls leave the machine.
"""

import argparse
import contextlib
import io
import json
import logging
import os
import statistics
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
from moto.server import ThreadedMotoServer

HERE = Path(__file__).parent
START = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
MOTO_PORT = 5155
TABLE = "tailhook-evals"
MERCHANT = {"fixed": False}


def start_merchant() -> ThreadingHTTPServer:
    from app.api import merchant_reply

    class Merchant(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
            status, text = merchant_reply(payload, MERCHANT["fixed"])
            self.send_response(status)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Merchant)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reset_table() -> None:
    from app import aws
    from tests.aws_fixtures import make_table

    client = boto3.client("dynamodb")
    with contextlib.suppress(client.exceptions.ResourceNotFoundException):
        client.delete_table(TableName=TABLE)
    make_table(TABLE)
    aws.reset()


def load(events: list[dict], merchant_fixed: bool) -> None:
    """Run each event through the worker in arrival order, retrying failures like SQS would."""
    from app import simulator, store, worker

    store.set_merchant_fixed(merchant_fixed)
    MERCHANT["fixed"] = merchant_fixed
    arrivals = []
    for spec in events:
        created = START + timedelta(seconds=spec["t"])
        body = simulator.event_body(
            spec["type"],
            spec["payment_id"],
            created,
            attempt=spec.get("attempt", 1),
            code=spec.get("code"),
            note=spec.get("note"),
        )
        for copy in range(spec.get("copies", 1)):
            arrived = created + timedelta(seconds=spec.get("delay", 0) + 5 * copy)
            arrivals.append((arrived, spec["id"], body, f"{spec['id']}-r{copy}"))

    for arrived, msg_id, body, receipt in sorted(arrivals):
        message = json.dumps({"id": msg_id, "body": body, "receipt": receipt})
        for attempt in range(1, worker.MAX_ATTEMPTS + 1):
            record = {
                "messageId": receipt,
                "receiptHandle": "-",
                "body": message,
                "attributes": {"ApproximateReceiveCount": str(attempt)},
            }
            if worker.process(record, now=arrived):
                break


def grade(case: dict, outcome: dict) -> list[str]:
    answer = outcome["answer"].lower()
    called = {step["tool"] for step in outcome["steps"]}
    failures = []
    for wanted in case.get("must_include", []):
        options = wanted if isinstance(wanted, list) else [wanted]
        if not any(option.lower() in answer for option in options):
            failures.append(f"missing {' / '.join(options)!r}")
    failures += [
        f"didn't cite {i}" for i in case.get("must_cite", []) if i not in outcome["answer"]
    ]
    failures += [f"didn't call {t}" for t in case.get("must_call", []) if t not in called]
    failures += [f"said {s!r}" for s in case.get("must_not_include", []) if s.lower() in answer]
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Grade the agent against evals/cases.json.")
    parser.add_argument("--model", choices=["haiku", "opus"], default="haiku")
    parser.add_argument("--only", help="run one case by name")
    args = parser.parse_args()

    spec = json.loads((HERE / "cases.json").read_text())
    cases = [case for case in spec["cases"] if args.only in (None, case["name"])]
    if not cases:
        print(f"no case named {args.only}", file=sys.stderr)
        return 2

    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # moto logs every request
    moto = ThreadedMotoServer(ip_address="127.0.0.1", port=MOTO_PORT, verbose=False)
    moto.start()
    merchant = start_merchant()
    os.environ.update(
        TABLE_NAME=TABLE,
        AWS_ENDPOINT_URL_DYNAMODB=f"http://127.0.0.1:{MOTO_PORT}",
        DESTINATION_URL=f"http://127.0.0.1:{merchant.server_port}/",
        OUTBOUND_SECRET="whsec_c2VjcmV0LWZvci1kZWxpdmVyaWVzLXRvLXRoZS1hcHA=",
    )
    from app import agent

    results = []
    try:
        for case in cases:
            reset_table()
            with contextlib.redirect_stdout(io.StringIO()):
                load(spec["background"] + case["events"], case.get("merchant_fixed", False))
            outcome = agent.ask(case["question"], model=args.model)
            failures = grade(case, outcome)
            results.append(
                {
                    "name": case["name"],
                    "group": case["group"],
                    "question": case["question"],
                    "passed": not failures,
                    "failures": failures,
                    **outcome,
                }
            )
            print(f"{'PASS' if not failures else 'FAIL'}  {case['name']}  {'; '.join(failures)}")
    finally:
        merchant.shutdown()
        moto.stop()

    passed = sum(result["passed"] for result in results)
    summary = {
        "model": agent.MODELS[args.model],
        "passed": passed,
        "total": len(results),
        "pass_rate": round(passed / len(results), 3),
        "mean_cost_usd": round(statistics.mean(r["cost_usd"] for r in results), 5),
        "median_seconds": round(statistics.median(r["ms"] for r in results) / 1000, 1),
    }
    out = HERE / "results" / f"{args.model}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "cases": results}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
