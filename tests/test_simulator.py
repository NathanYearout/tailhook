import asyncio
import json
import os
from datetime import UTC, datetime

from app import signing, simulator
from app.declines import DECLINE_CODES

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
TYPES = {"payment.succeeded", "payment.failed", "refund.created", "dispute.opened"}


def test_event_body_shape():
    body = simulator.event_body("payment.failed", "pay_1", T0, code="51", note="hi", run="abc")

    assert json.loads(body) == {
        "type": "payment.failed",
        "timestamp": "2026-10-12T14:00:00.000Z",
        "data": {
            "payment_id": "pay_1",
            "amount": 4200,
            "currency": "USD",
            "card": {"brand": "visa", "last4": "4242"},
            "attempt": 1,
            "decline": {"code": "51", "reason": "insufficient_funds"},
            "note": "hi",
            "run": "abc",
        },
    }


def test_mixed_is_the_same_for_the_same_seed():
    assert simulator.mixed(7, T0) == simulator.mixed(7, T0)
    assert simulator.mixed(7, T0) != simulator.mixed(8, T0)


def test_mixed_events_are_well_formed():
    for delivery in simulator.mixed(7, T0):
        payload = json.loads(delivery.body)
        assert payload["type"] in TYPES
        assert payload["data"]["payment_id"].startswith("pay_")
        if payload["type"] == "payment.failed":
            assert payload["data"]["decline"]["code"] in DECLINE_CODES


def test_mixed_has_duplicates_and_a_refund_that_overtakes_its_payment():
    sends = simulator.mixed(7, T0)
    ids = [d.msg_id for d in sends]
    assert len(ids) - len(set(ids)) == 3

    first_sent = {}
    for position, delivery in enumerate(sends):
        payload = json.loads(delivery.body)
        first_sent.setdefault((payload["data"]["payment_id"], payload["type"]), position)
    refunded = [pid for pid, kind in first_sent if kind == "refund.created"]
    assert len(refunded) == 2
    assert any(
        first_sent[(pid, "refund.created")] < first_sent[(pid, "payment.succeeded")]
        for pid in refunded
    )


def test_send_posts_signed_events_in_order(destination):
    deliveries = simulator.mixed(7, T0)[:3]

    assert simulator.send(destination.url, os.environ["INBOUND_SECRET"], deliveries) == {200: 3}
    for (headers, body), delivery in zip(destination.received, deliveries, strict=True):
        assert body == delivery.body
        signing.verify(os.environ["INBOUND_SECRET"], headers, body)


def test_load_sends_unique_signed_events_and_reports_latency(destination):
    result = asyncio.run(simulator.load(destination.url, os.environ["INBOUND_SECRET"], 20, 1, "t1"))

    assert result["sent"] == 20
    assert result["statuses"] == {"200": 20}
    assert result["p50_ms"] <= result["p99_ms"]
    assert len({headers["webhook-id"] for headers, _ in destination.received}) == 20
