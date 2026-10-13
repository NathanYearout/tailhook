"""The dashboard's API, the simulator trigger, and the demo's stand-in merchant app."""

import json
import os
import random

from app import aws, signing, simulator, store, web

DAILY_QUESTIONS = 100


def handler(event, context):
    route = ROUTES.get(event["routeKey"])
    if route is None:
        return web.error(404, "not found")
    return route(event)


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
    deliveries = simulator.mixed(random.randrange(1_000_000), store.utcnow())
    responses = simulator.send(os.environ["WEBHOOK_URL"], os.environ["INBOUND_SECRET"], deliveries)
    return web.response(200, {"sent": len(deliveries), "responses": dict(responses)})


def set_merchant(event):
    body = _json_body(event)
    if not isinstance(body, dict) or not isinstance(body.get("fixed"), bool):
        return web.error(400, 'send {"fixed": true} or {"fixed": false}')
    store.set_merchant_fixed(body["fixed"])
    return web.response(200, {"fixed": body["fixed"]})


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
    "GET /api/events": list_events,
    "GET /api/events/{id}": show_event,
    "GET /api/payments/{id}": show_payment,
    "GET /api/stats": stats,
    "POST /api/simulate": simulate,
    "POST /api/merchant": set_merchant,
    "POST /demo/merchant": demo_merchant,
}
