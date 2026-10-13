"""Drains the events queue: store each event once, deliver it, back off when delivery fails."""

import json
import os
import traceback
from datetime import UTC, datetime

from app import aws, delivery, store

MAX_ATTEMPTS = 3  # matches maxReceiveCount in the queue's redrive policy


def handler(event, context):
    failures = []
    for record in event["Records"]:
        try:
            ok = process(record)
        except Exception:  # don't let one bad message fail the whole batch
            traceback.print_exc()
            ok = False
        if not ok:
            failures.append({"itemIdentifier": record["messageId"]})
            backoff(record)
    return {"batchItemFailures": failures}


def process(record: dict, now=None) -> bool:
    """Handle one SQS message. True means it's finished and can leave the queue."""
    message = json.loads(record["body"])
    # arrival time is when ingest queued it, not when a worker got around to it
    now = now or datetime.fromtimestamp(int(record["attributes"]["SentTimestamp"]) / 1000, UTC)
    item, created = store.record_receipt(message["id"], message["body"], message["receipt"], now)
    if created:
        log("stored", item)
    if item["owner"] != message["receipt"]:
        log("duplicate", item)
        return True
    if item["status"] == "delivered":
        return True  # SQS handed us a message we already finished

    attempt = int(record["attributes"]["ApproximateReceiveCount"])
    failed_status = "dead" if attempt >= MAX_ATTEMPTS else "retrying"
    ok = delivery.deliver(item, failed_status=failed_status)
    log("delivered" if ok else failed_status, item)
    return ok


def backoff(record: dict) -> None:
    # 2 s, 4 s, then 8 s; after the third failure SQS moves it to the dead-letter queue.
    attempt = int(record["attributes"]["ApproximateReceiveCount"])
    aws.sqs().change_message_visibility(
        QueueUrl=os.environ["QUEUE_URL"],
        ReceiptHandle=record["receiptHandle"],
        VisibilityTimeout=2**attempt,
    )


def log(outcome: str, item: dict) -> None:
    run = json.loads(item["payload"])["data"].get("run")
    line = {"outcome": outcome, "id": item["id"], "payment_id": item["payment_id"], "run": run}
    print(json.dumps(line))
