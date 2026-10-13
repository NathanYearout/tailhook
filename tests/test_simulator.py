import asyncio
import json
import os
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def test_mixed_has_duplicates_and_an_early_refund():
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


def test_run_load_splits_across_processes(destination):
    result = simulator.run_load(destination.url, os.environ["INBOUND_SECRET"], 20, 1, workers=2)

    assert result["sent"] == 20
    assert result["statuses"] == {"200": 20}
    assert result["p50_ms"] <= result["p99_ms"]
    ids = [headers["webhook-id"] for headers, _ in destination.received]
    assert len(set(ids)) == 20
    assert all(i.startswith(f"msg_{result['run']}_") for i in ids)


def test_load_caps_requests_in_flight(monkeypatch):
    # a slow app makes requests pile up; they should wait on our side, not in httpx's pool
    monkeypatch.setattr(simulator, "MAX_IN_FLIGHT", 5)
    lock, live, peak = threading.Lock(), [0], [0]

    class SlowApp(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.1)
            with lock:
                live[0] -= 1
            self.send_response(202)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowApp)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        result = asyncio.run(simulator.load(url, os.environ["INBOUND_SECRET"], 100, 1, "t2"))
    finally:
        server.shutdown()

    assert result["statuses"] == {202: 100}
    assert peak[0] <= 5
