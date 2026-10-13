import json
import os
from datetime import UTC, datetime

from app import aws, store, worker
from app.simulator import event_body

BODY = event_body("payment.succeeded", "pay_1", datetime(2026, 10, 12, 14, 0, tzinfo=UTC))


def record(receipt="r1", attempt=1, msg_id="msg_1"):
    return {
        "messageId": f"{receipt}-{attempt}",
        "receiptHandle": "unused",
        "body": json.dumps({"id": msg_id, "body": BODY, "receipt": receipt}),
        "attributes": {
            "ApproximateReceiveCount": str(attempt),
            "SentTimestamp": "1791813600000",  # 2026-10-12T14:00:00Z, when ingest queued it
        },
    }


def sqs_batch(*bodies):
    """Queue the bodies and receive them back as the records of one Lambda event."""
    sqs, queue = aws.sqs(), os.environ["QUEUE_URL"]
    for body in bodies:
        sqs.send_message(QueueUrl=queue, MessageBody=body)
    messages = sqs.receive_message(
        QueueUrl=queue, MaxNumberOfMessages=10, MessageSystemAttributeNames=["All"]
    )["Messages"]
    messages.sort(key=lambda m: bodies.index(m["Body"]))
    return {
        "Records": [
            {
                "messageId": m["MessageId"],
                "receiptHandle": m["ReceiptHandle"],
                "body": m["Body"],
                "attributes": m["Attributes"],
            }
            for m in messages
        ]
    }


def test_first_copy_stored_and_delivered(destination, capsys):
    assert worker.process(record())

    assert len(destination.received) == 1
    assert store.get_event("msg_1")["status"] == "delivered"
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["outcome"] for line in logged] == ["stored", "delivered"]


def test_sender_duplicate_not_delivered(destination):
    worker.process(record("r1"))

    assert worker.process(record("r2"))
    assert len(destination.received) == 1
    assert store.get_event("msg_1")["receipts"] == {"r1", "r2"}


def test_duplicate_during_retry_not_delivered(destination):
    destination.replies.append(500)

    assert not worker.process(record("r1"))
    assert worker.process(record("r2"))
    assert len(destination.received) == 1


def test_redelivery_after_failure_retries(destination):
    destination.replies.append(500)

    assert not worker.process(record("r1", attempt=1))
    assert store.get_event("msg_1")["status"] == "retrying"
    assert worker.process(record("r1", attempt=2))

    item = store.get_event("msg_1")
    assert item["status"] == "delivered"
    assert [a["status_code"] for a in item["attempts"]] == [500, 200]


def test_redelivery_after_success_is_noop(destination):
    worker.process(record("r1", attempt=1))

    assert worker.process(record("r1", attempt=2))
    assert len(destination.received) == 1


def test_third_failure_is_dead(destination):
    destination.replies.extend([500, 500, 500])

    for attempt in (1, 2, 3):
        assert not worker.process(record("r1", attempt=attempt))
    assert store.get_event("msg_1")["status"] == "dead"


def test_handler_reports_failures(destination):
    destination.replies.append(500)
    sqs, queue = aws.sqs(), os.environ["QUEUE_URL"]
    for n in (1, 2):
        message = {"id": f"msg_{n}", "body": BODY, "receipt": f"r{n}"}
        sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(message))
    messages = sqs.receive_message(
        QueueUrl=queue, MaxNumberOfMessages=10, MessageSystemAttributeNames=["All"]
    )["Messages"]
    event = {
        "Records": [
            {
                "messageId": m["MessageId"],
                "receiptHandle": m["ReceiptHandle"],
                "body": m["Body"],
                "attributes": m["Attributes"],
            }
            for m in messages
        ]
    }

    result = worker.handler(event, None)

    assert len(result["batchItemFailures"]) == 1


def test_received_at_uses_sent_timestamp(destination):
    worker.process(record())

    assert store.get_event("msg_1")["received_at"] == "2026-10-12T14:00:00.000Z"


def test_redelivery_not_logged_as_stored(destination, capsys):
    destination.replies.append(500)

    worker.process(record(attempt=1))
    worker.process(record(attempt=2))

    outcomes = [json.loads(line)["outcome"] for line in capsys.readouterr().out.splitlines()]
    assert outcomes.count("stored") == 1


def test_bad_message_doesnt_fail_batch(destination):
    good = json.dumps({"id": "msg_2", "body": BODY, "receipt": "r2"})
    event = sqs_batch("not json", good)

    result = worker.handler(event, None)

    assert result["batchItemFailures"] == [{"itemIdentifier": event["Records"][0]["messageId"]}]
    assert store.get_event("msg_2")["status"] == "delivered"
