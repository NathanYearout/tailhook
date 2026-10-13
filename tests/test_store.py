from datetime import UTC, datetime, timedelta

from app import store
from app.simulator import event_body

T0 = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)


def body(event_type="payment.succeeded", payment_id="pay_1", at=T0, **fields):
    return event_body(event_type, payment_id, at, **fields)


def later(seconds):
    return T0 + timedelta(seconds=seconds)


def attempt(status_code=200):
    return {
        "at": store.iso(T0),
        "status_code": status_code,
        "ms": 12,
        "body": "ok",
        "replay": False,
    }


def test_the_first_receipt_creates_the_event_and_owns_delivery():
    item, created = store.record_receipt("msg_1", body(), "r1", now=T0)

    assert created
    assert item["owner"] == "r1"
    assert item["status"] == "pending"
    assert item["receipts"] == {"r1"}
    assert item["attempts"] == []
    assert item["payment_id"] == "pay_1"
    assert item["created_at"] == store.iso(T0)
    assert item["shard"] == store.shard_for("msg_1")


def test_a_second_receipt_is_counted_but_does_not_take_over():
    store.record_receipt("msg_1", body(), "r1", now=T0)
    item, created = store.record_receipt("msg_1", body(), "r2", now=later(3))

    assert not created
    assert item["owner"] == "r1"
    assert item["receipts"] == {"r1", "r2"}
    assert item["received_at"] == store.iso(T0)


def test_the_same_receipt_twice_is_counted_once():
    store.record_receipt("msg_1", body(), "r1", now=T0)
    item, created = store.record_receipt("msg_1", body(), "r1", now=later(9))

    assert not created
    assert item["receipts"] == {"r1"}


def test_add_attempt_appends_and_sets_status():
    store.record_receipt("msg_1", body(), "r1", now=T0)
    store.add_attempt("msg_1", attempt(500), "retrying")
    store.add_attempt("msg_1", attempt(200), "delivered")

    item = store.get_event("msg_1")
    assert item["status"] == "delivered"
    assert [a["status_code"] for a in item["attempts"]] == [500, 200]


def test_get_event_ignores_control_items_and_unknown_ids():
    store.set_merchant_fixed(True)
    assert store.get_event("config#merchant") is None
    assert store.get_event("msg_missing") is None


def test_timeline_is_in_created_order_and_flags_early_arrivals():
    # The refund was created after the payment but reached us first.
    store.record_receipt("msg_paid", body("payment.succeeded", at=T0), "r1", now=later(60))
    store.record_receipt("msg_refund", body("refund.created", at=later(30)), "r2", now=later(31))

    rows = store.timeline("pay_1")

    assert [r["id"] for r in rows] == ["msg_paid", "msg_refund"]
    assert [r["arrived"] for r in rows] == [2, 1]
    assert [r["out_of_order"] for r in rows] == [False, True]


def test_recent_merges_shards_newest_first():
    for n in range(12):
        store.record_receipt(f"msg_{n}", body(payment_id=f"pay_{n}"), f"r{n}", now=later(n))

    assert [i["id"] for i in store.recent(3)] == ["msg_11", "msg_10", "msg_9"]
    assert [i["id"] for i in store.recent(50, since=store.iso(later(10)))] == ["msg_11", "msg_10"]


def test_recent_filters_by_type_and_status():
    store.record_receipt("msg_paid", body(), "r1", now=T0)
    store.record_receipt("msg_failed", body("payment.failed", code="51"), "r2", now=later(1))
    store.add_attempt("msg_failed", attempt(500), "dead")

    assert [i["id"] for i in store.recent(event_type="payment.failed")] == ["msg_failed"]
    assert [i["id"] for i in store.recent(status="dead")] == ["msg_failed"]
    assert store.recent(status="delivered") == []


def test_summary_and_detail():
    store.record_receipt("msg_1", body("payment.failed", code="51"), "r1", now=T0)
    store.add_attempt("msg_1", attempt(200), "delivered")
    item = store.get_event("msg_1")

    assert store.summary(item) == {
        "id": "msg_1",
        "type": "payment.failed",
        "payment_id": "pay_1",
        "created_at": store.iso(T0),
        "received_at": store.iso(T0),
        "copies_received": 1,
        "status": "delivered",
        "attempts": 1,
        "decline_code": "51",
    }
    full = store.detail(item)
    assert full["payload"]["data"]["decline"] == {"code": "51", "reason": "insufficient_funds"}
    assert full["delivery_attempts"][0]["status_code"] == 200


def test_the_merchant_switch_starts_broken():
    assert not store.merchant_fixed()
    store.set_merchant_fixed(True)
    assert store.merchant_fixed()


def test_the_question_budget_stops_at_the_cap():
    assert [store.take_question("2026-10-12", cap=2) for _ in range(3)] == [True, True, False]
    assert store.questions_asked("2026-10-12") == 2
    assert store.questions_asked("2026-10-13") == 0
