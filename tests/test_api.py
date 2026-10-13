import json
import os
from datetime import UTC, datetime, timedelta

from app import api, signing, store, worker
from app.simulator import event_body

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def call(route, body=None, path=None, query=None, headers=None, raw=None):
    event = {
        "routeKey": route,
        "pathParameters": path or {},
        "queryStringParameters": query,
        "headers": headers or {},
        "body": raw if raw is not None else (None if body is None else json.dumps(body)),
        "isBase64Encoded": False,
    }
    response = api.handler(event, None)
    if response["headers"]["content-type"] == "application/json":
        return response["statusCode"], json.loads(response["body"])
    return response["statusCode"], response["body"]


def receive(msg_id, event_type="payment.succeeded", payment_id="pay_1", at=0, **fields):
    """Run an event through the worker as if it had come off the queue."""
    body = event_body(event_type, payment_id, T0 + timedelta(seconds=at), **fields)
    message = {"id": msg_id, "body": body, "receipt": f"r-{msg_id}"}
    worker.process(
        {
            "messageId": msg_id,
            "receiptHandle": "-",
            "body": json.dumps(message),
            "attributes": {"ApproximateReceiveCount": "1"},
        }
    )
    return body


def outbound(body):
    return signing.signed_headers(os.environ["OUTBOUND_SECRET"], "msg_1", body)


def test_unknown_routes_are_404():
    assert call("GET /nope")[0] == 404


def test_list_events_newest_first(destination):
    receive("msg_a")
    receive("msg_b")

    status, rows = call("GET /api/events", query={"limit": "1"})

    assert status == 200
    assert [row["id"] for row in rows] == ["msg_b"]


def test_list_events_rejects_a_bad_limit():
    assert call("GET /api/events", query={"limit": "lots"})[0] == 400


def test_show_event(destination):
    receive("msg_a")

    status, event = call("GET /api/events/{id}", path={"id": "msg_a"})

    assert status == 200
    assert event["payload"]["data"]["payment_id"] == "pay_1"
    assert event["delivery_attempts"][0]["status_code"] == 200
    assert call("GET /api/events/{id}", path={"id": "config#merchant"})[0] == 404


def test_show_payment_in_created_order(destination):
    receive("msg_refund", "refund.created", at=30)
    receive("msg_paid", "payment.succeeded", at=0)

    status, rows = call("GET /api/payments/{id}", path={"id": "pay_1"})

    assert status == 200
    assert [row["id"] for row in rows] == ["msg_paid", "msg_refund"]
    assert [row["out_of_order"] for row in rows] == [False, True]
    assert call("GET /api/payments/{id}", path={"id": "pay_none"})[0] == 404


def test_stats():
    assert call("GET /api/stats") == (
        200,
        {"dead_letters": 0, "merchant_fixed": False, "questions_left": api.DAILY_QUESTIONS},
    )


def test_the_merchant_switch():
    assert call("POST /api/merchant", {"fixed": True}) == (200, {"fixed": True})
    assert store.merchant_fixed()
    assert call("POST /api/merchant", {"fixed": "yes"})[0] == 400
    assert call("POST /api/merchant", raw="{nope")[0] == 400


def test_simulate_sends_the_mixed_scenario(destination, monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", destination.url)

    status, body = call("POST /api/simulate")

    assert status == 200
    assert body["responses"] == {"200": body["sent"]}
    assert len(destination.received) == body["sent"]


def test_the_demo_merchant_fails_refunds_until_the_fix_ships():
    refund = event_body("refund.created", "pay_1", T0)

    assert call("POST /demo/merchant", headers=outbound(refund), raw=refund) == (
        500,
        "KeyError: 'original_amount'",
    )
    store.set_merchant_fixed(True)
    assert call("POST /demo/merchant", headers=outbound(refund), raw=refund) == (200, "ok")


def test_the_demo_merchant_checks_signatures():
    paid = event_body("payment.succeeded", "pay_1", T0)

    assert call("POST /demo/merchant", headers=outbound(paid), raw=paid) == (200, "ok")
    assert call("POST /demo/merchant", headers={}, raw=paid)[0] == 401
