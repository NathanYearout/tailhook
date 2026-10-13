"""Creates the table and queues the way infra/main.tf does, for moto."""

import json

import boto3

KEY_ATTRIBUTES = ("id", "payment_id", "created_at", "shard", "received_at")


def _index(name: str, hash_key: str, range_key: str) -> dict:
    return {
        "IndexName": name,
        "KeySchema": [
            {"AttributeName": hash_key, "KeyType": "HASH"},
            {"AttributeName": range_key, "KeyType": "RANGE"},
        ],
        "Projection": {"ProjectionType": "ALL"},
    }


def make_table(name: str) -> None:
    boto3.client("dynamodb").create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in KEY_ATTRIBUTES],
        GlobalSecondaryIndexes=[
            _index("by_payment", "payment_id", "created_at"),
            _index("feed", "shard", "received_at"),
        ],
    )


def make_queues() -> tuple[str, str]:
    sqs = boto3.client("sqs")
    dead = sqs.create_queue(QueueName="tailhook-dead")["QueueUrl"]
    dead_attributes = sqs.get_queue_attributes(QueueUrl=dead, AttributeNames=["QueueArn"])
    redrive = {
        "deadLetterTargetArn": dead_attributes["Attributes"]["QueueArn"],
        "maxReceiveCount": "3",
    }
    events = sqs.create_queue(
        QueueName="tailhook-events",
        Attributes={"VisibilityTimeout": "360", "RedrivePolicy": json.dumps(redrive)},
    )["QueueUrl"]
    return events, dead
