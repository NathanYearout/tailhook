import base64
import json
import os
from datetime import UTC, datetime

from app import aws, ingest, signing
from app.simulator import event_body

BODY = event_body("payment.succeeded", "pay_1", datetime(2026, 10, 12, 14, 0, tzinfo=UTC))


def request(body=BODY, headers=None, base64_encoded=False):
    if headers is None:
        headers = signing.signed_headers(os.environ["INBOUND_SECRET"], "msg_1", body)
    raw = base64.b64encode(body.encode()).decode() if base64_encoded else body
    return {
        "body": raw,
        "isBase64Encoded": base64_encoded,
        "headers": headers,
        "requestContext": {"requestId": "req-1"},
    }


def queued():
    messages = aws.sqs().receive_message(QueueUrl=os.environ["QUEUE_URL"], MaxNumberOfMessages=10)
    return [json.loads(m["Body"]) for m in messages.get("Messages", [])]


def payload(payment_id):
    return json.dumps(
        {
            "type": "payment.succeeded",
            "timestamp": "2026-10-12T14:00:00.000Z",
            "data": {"payment_id": payment_id},
        }
    )


def test_signed_event_is_queued():
    response = ingest.handler(request(), None)

    assert response["statusCode"] == 202
    assert json.loads(response["body"]) == {"receipt": "req-1"}
    assert queued() == [{"id": "msg_1", "body": BODY, "receipt": "req-1"}]


def test_bad_signature():
    wrong = "whsec_" + base64.b64encode(b"wrong").decode()
    headers = signing.signed_headers(wrong, "msg_1", BODY)

    assert ingest.handler(request(headers=headers), None)["statusCode"] == 401
    assert queued() == []


def test_missing_headers():
    assert ingest.handler(request(headers={"webhook-id": "msg_1"}), None)["statusCode"] == 400
    assert queued() == []


def test_missing_payment_id():
    body = json.dumps({"type": "payment.succeeded", "timestamp": "2026-10-12T14:00:00.000Z"})
    assert ingest.handler(request(body), None)["statusCode"] == 400


def test_payment_id_must_be_a_string():
    for payment_id in (None, 42, ""):
        assert ingest.handler(request(payload(payment_id)), None)["statusCode"] == 400
    assert queued() == []


def test_body_not_json():
    assert ingest.handler(request("not json"), None)["statusCode"] == 400


def test_oversized_body_rejected_first():
    huge = "x" * (200 * 1024 + 1)
    assert ingest.handler(request(huge, headers={}), None)["statusCode"] == 413


def test_non_ascii_body_verifies():
    body = json.dumps(
        {
            "type": "payment.succeeded",
            "timestamp": "2026-10-12T14:00:00.000Z",
            "data": {"payment_id": "pay_ü", "note": "café ☕"},
        },
        ensure_ascii=False,
    )

    assert ingest.handler(request(body), None)["statusCode"] == 202
    assert queued()[0]["body"] == body


def test_base64_body():
    assert ingest.handler(request(base64_encoded=True), None)["statusCode"] == 202
    assert queued()[0]["body"] == BODY


def test_key_sizes():
    secret, long = os.environ["INBOUND_SECRET"], "x" * 300
    late = json.dumps({"type": "payment.succeeded", "timestamp": long, "data": {"payment_id": "p"}})

    assert ingest.handler(request(payload(long)), None)["statusCode"] == 400
    assert ingest.handler(request(late), None)["statusCode"] == 400
    for msg_id in ("msg_" + long, ""):
        headers = signing.signed_headers(secret, msg_id, BODY)
        assert ingest.handler(request(headers=headers), None)["statusCode"] == 400
    assert queued() == []


def test_multibyte_text_not_escaped():
    note = "café ☕ " * 9000
    data = {"payment_id": "pay_1", "note": note}
    event = {"type": "payment.succeeded", "timestamp": "2026-10-12T14:00:00.000Z", "data": data}
    body = json.dumps(event, ensure_ascii=False)

    assert ingest.handler(request(body), None)["statusCode"] == 202
    raw = aws.sqs().receive_message(QueueUrl=os.environ["QUEUE_URL"])["Messages"][0]["Body"]
    assert len(raw.encode()) < 1.1 * len(body.encode())
