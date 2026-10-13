"""The dashboard's API, the simulator trigger, and the demo's stand-in merchant app."""

import json
import os
import random
import time
from datetime import timedelta
from functools import cache
from pathlib import Path

import anthropic

from app import agent, aws, delivery, signing, simulator, store, web

DAILY_QUESTIONS = 100
MAX_QUESTION = 500
RETRY_BATCH = 25
RETRY_SECONDS = 20  # stay well inside API Gateway's 30 second limit


def handler(event, context):
    route = ROUTES.get(event["routeKey"])
    if route is None:
        return web.error(404, "not found")
    return route(event)


@cache
def _page() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text()


def dashboard(event):
    return web.response(200, _page(), "text/html; charset=utf-8")


def list_events(event):
    query = event.get("queryStringParameters") or {}
    try:
        limit = min(int(query.get("limit", 50)), 100)
    except ValueError:
        return web.error(400, "limit must be a whole number")
    return web.response(200, [store.summary(item) for item in store.recent(limit)])


def show_event(event):
    item = store.get_event(event["pathParameters"]["id"])
    if item is None:
        return web.error(404, "no event with that id")
    return web.response(200, store.detail(item))


def show_payment(event):
    items = store.timeline(event["pathParameters"]["id"])
    if not items:
        return web.error(404, "no events for that payment")
    return web.response(200, [store.summary(item) for item in items])


def replay_payment(event):
    items = store.timeline(event["pathParameters"]["id"])
    if not items:
        return web.error(404, "no events for that payment")
    results = []
    for item in items:
        ok = delivery.deliver(item, failed_status=item["status"], replay=True)
        results.append({"id": item["id"], "type": item["type"], "ok": ok})
        if not ok:
            break  # later events must not overtake the one that failed
    complete = len(results) == len(items) and results[-1]["ok"]
    return web.response(200, {"results": results, "complete": complete})


def retry_dead_letters(event):
    sqs, queue = aws.sqs(), os.environ["DLQ_URL"]
    messages, drained = [], False
    while len(messages) < RETRY_BATCH:
        batch = sqs.receive_message(
            QueueUrl=queue,
            MaxNumberOfMessages=min(10, RETRY_BATCH - len(messages)),
            VisibilityTimeout=60,
            WaitTimeSeconds=1,  # a short poll can come back empty while messages remain
        ).get("Messages", [])
        if not batch:
            drained = True
            break
        messages += batch

    pending = []
    for message in messages:
        queued = json.loads(message["Body"])
        # rows expire after 7 days, dead letters after 14; rebuild the row from the message
        item = store.get_event(queued["id"])
        if item is None:
            item, _ = store.record_receipt(queued["id"], queued["body"], queued["receipt"])
        pending.append((item, message))
    pending.sort(key=lambda pair: pair[0]["created_at"])
    deadline = time.monotonic() + RETRY_SECONDS
    delivered = failed = skipped = 0
    for item, message in pending:
        handle = message["ReceiptHandle"]
        if time.monotonic() > deadline:
            sqs.change_message_visibility(QueueUrl=queue, ReceiptHandle=handle, VisibilityTimeout=0)
            skipped += 1
        elif item["status"] == "delivered" or delivery.deliver(
            item, failed_status="dead", replay=True
        ):
            sqs.delete_message(QueueUrl=queue, ReceiptHandle=handle)
            delivered += 1
        else:
            sqs.change_message_visibility(QueueUrl=queue, ReceiptHandle=handle, VisibilityTimeout=0)
            failed += 1
    # SQS's count lags deletes, so if we emptied the queue, count what we put back
    left = failed + skipped if drained else _dead_letter_count()
    return web.response(200, {"delivered": delivered, "failed": failed, "left": left})


def stats(event):
    return web.response(
        200,
        {
            "dead_letters": _dead_letter_count(),
            "merchant_fixed": store.merchant_fixed(),
            "questions_left": max(0, DAILY_QUESTIONS - store.questions_asked(_today())),
        },
    )


def simulate(event):
    # mixed() spans up to 10 minutes from start; keep created_at in the past
    start = store.utcnow() - timedelta(minutes=10)
    deliveries = simulator.mixed(random.randrange(1_000_000), start)
    responses = simulator.send(os.environ["WEBHOOK_URL"], os.environ["INBOUND_SECRET"], deliveries)
    return web.response(200, {"sent": len(deliveries), "responses": dict(responses)})


def set_merchant(event):
    body = _json_body(event)
    if not isinstance(body, dict) or not isinstance(body.get("fixed"), bool):
        return web.error(400, 'send {"fixed": true} or {"fixed": false}')
    store.set_merchant_fixed(body["fixed"])
    return web.response(200, {"fixed": body["fixed"]})


def ask(event):
    body = _json_body(event)
    question = body.get("question") if isinstance(body, dict) else None
    if not isinstance(question, str) or not question.strip():
        return web.error(400, 'send {"question": "..."}')
    if len(question) > MAX_QUESTION:
        return web.error(400, f"keep questions under {MAX_QUESTION} characters")
    if not store.take_question(_today(), DAILY_QUESTIONS):
        return web.error(429, "the demo's daily question budget is used up, try again tomorrow")
    try:
        return web.response(200, agent.ask(question.strip()))
    except anthropic.APIError as error:
        print(f"agent call failed: {error!r}")
        return web.error(502, "the model is unavailable right now")


def demo_merchant(event):
    """Plays the merchant's app in the demo, checking signatures the way a real one should."""
    body = web.raw_body(event)
    try:
        signing.verify(os.environ["OUTBOUND_SECRET"], event.get("headers") or {}, body)
    except signing.InvalidSignature as error:
        return web.error(401, str(error))
    status, text = merchant_reply(json.loads(body), store.merchant_fixed())
    return web.response(status, text, "text/plain")


def merchant_reply(payload: dict, fixed: bool) -> tuple[int, str]:
    # The demo's planted bug: refunds crash the app until someone ships the fix.
    if payload["type"] == "refund.created" and not fixed:
        return 500, "KeyError: 'original_amount'"
    return 200, "ok"


def _json_body(event):
    try:
        return json.loads(web.raw_body(event))
    except ValueError:
        return None


def _today() -> str:
    return store.utcnow().date().isoformat()


def _dead_letter_count() -> int:
    attributes = aws.sqs().get_queue_attributes(
        QueueUrl=os.environ["DLQ_URL"], AttributeNames=["ApproximateNumberOfMessages"]
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


ROUTES = {
    "GET /": dashboard,
    "GET /api/events": list_events,
    "GET /api/events/{id}": show_event,
    "GET /api/payments/{id}": show_payment,
    "POST /api/payments/{id}/replay": replay_payment,
    "POST /api/dead-letters/retry": retry_dead_letters,
    "GET /api/stats": stats,
    "POST /api/simulate": simulate,
    "POST /api/merchant": set_merchant,
    "POST /api/ask": ask,
    "POST /demo/merchant": demo_merchant,
}
