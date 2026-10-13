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
        "attributes": {"ApproximateReceiveCount": str(attempt)},
    }


def test_the_first_copy_is_stored_and_delivered(destination, capsys):
    assert worker.process(record())

    assert len(destination.received) == 1
    assert store.get_event("msg_1")["status"] == "delivered"
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["outcome"] for line in logged] == ["stored", "delivered"]


def test_a_sender_duplicate_is_counted_but_not_delivered(destination):
    worker.process(record("r1"))

    assert worker.process(record("r2"))
    assert len(destination.received) == 1
    assert store.get_event("msg_1")["receipts"] == {"r1", "r2"}


def test_a_duplicate_arriving_while_the_first_copy_is_retrying_is_not_delivered(destination):
    destination.replies.append(500)

    assert not worker.process(record("r1"))
    assert worker.process(record("r2"))
    assert len(destination.received) == 1


def test_sqs_redelivering_a_failed_message_retries_it(destination):
    destination.replies.append(500)

    assert not worker.process(record("r1", attempt=1))
    assert store.get_event("msg_1")["status"] == "retrying"
    assert worker.process(record("r1", attempt=2))

    item = store.get_event("msg_1")
    assert item["status"] == "delivered"
    assert [a["status_code"] for a in item["attempts"]] == [500, 200]


def test_sqs_redelivering_a_finished_message_does_nothing(destination):
    worker.process(record("r1", attempt=1))

    assert worker.process(record("r1", attempt=2))
    assert len(destination.received) == 1


def test_the_third_failure_marks_the_event_dead(destination):
    destination.replies.extend([500, 500, 500])

    for attempt in (1, 2, 3):
        assert not worker.process(record("r1", attempt=attempt))
    assert store.get_event("msg_1")["status"] == "dead"


def test_the_handler_reports_only_the_failed_message(destination):
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
