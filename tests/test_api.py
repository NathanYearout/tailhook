import json
import os
from datetime import UTC, datetime, timedelta

from app import api, aws, signing, store, worker
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


def test_replay_goes_in_created_order_and_stops_at_the_first_failure(destination):
    receive("msg_c", "refund.created", at=60)
    receive("msg_a", "payment.failed", at=0, code="51")
    receive("msg_b", "payment.succeeded", at=30, attempt=2)
    destination.received.clear()
    destination.replies.extend([200, 500])

    status, body = call("POST /api/payments/{id}/replay", path={"id": "pay_1"})

    assert status == 200
    assert [(r["id"], r["ok"]) for r in body["results"]] == [("msg_a", True), ("msg_b", False)]
    assert body["complete"] is False
    sent_types = [json.loads(sent)["type"] for _, sent in destination.received]
    assert sent_types == ["payment.failed", "payment.succeeded"]
    assert store.get_event("msg_b")["attempts"][-1]["replay"] is True


def test_replaying_an_unknown_payment_is_404():
    assert call("POST /api/payments/{id}/replay", path={"id": "pay_none"})[0] == 404


def dead_letter(msg_id, **event):
    """An event whose delivery gave up: marked dead, with its message in the DLQ."""
    body = receive(msg_id, **event)
    failed = {"at": "-", "status_code": 500, "ms": 1, "body": "boom", "replay": False}
    store.add_attempt(msg_id, failed, "dead")
    message = {"id": msg_id, "body": body, "receipt": f"r-{msg_id}"}
    aws.sqs().send_message(QueueUrl=os.environ["DLQ_URL"], MessageBody=json.dumps(message))


def test_retry_dead_letters_delivers_in_created_order_and_clears_the_queue(destination):
    dead_letter("msg_b", event_type="refund.created", at=30)
    dead_letter("msg_a", event_type="refund.created", payment_id="pay_2", at=0)
    destination.received.clear()

    assert call("POST /api/dead-letters/retry") == (200, {"delivered": 2, "failed": 0, "left": 0})
    sent = [json.loads(body)["data"]["payment_id"] for _, body in destination.received]
    assert sent == ["pay_2", "pay_1"]
    assert store.get_event("msg_a")["status"] == "delivered"


def test_retry_dead_letters_keeps_what_still_fails(destination):
    dead_letter("msg_a", event_type="refund.created")
    destination.received.clear()
    destination.replies.append(500)

    assert call("POST /api/dead-letters/retry")[1] == {"delivered": 0, "failed": 1, "left": 1}
    assert store.get_event("msg_a")["status"] == "dead"


def test_retry_dead_letters_skips_events_already_replayed(destination):
    dead_letter("msg_a", event_type="refund.created")
    call("POST /api/payments/{id}/replay", path={"id": "pay_1"})
    destination.received.clear()

    assert call("POST /api/dead-letters/retry")[1] == {"delivered": 1, "failed": 0, "left": 0}
    assert destination.received == []
