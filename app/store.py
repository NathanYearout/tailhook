"""What tailhook keeps in DynamoDB: events with their delivery attempts, and two control items."""

import functools
import json
import operator
import os
import zlib
from datetime import UTC, datetime, timedelta

from boto3.dynamodb.conditions import Attr, Key

from app import aws

SHARDS = 8
KEEP_FOR = timedelta(days=7)
KEEP_ATTEMPTS = 20
MERCHANT = {"id": "config#merchant"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def shard_for(event_id: str) -> str:
    return f"feed#{zlib.crc32(event_id.encode()) % SHARDS}"


def record_receipt(
    event_id: str, body: str, receipt: str, now: datetime | None = None
) -> tuple[dict, bool]:
    """Store the event if it's new and add this receipt to it, in one atomic write.

    Returns the item as it is after the write, and whether this call created it. The
    receipt that created it owns delivery; every other receipt only gets counted.
    """
    now = now or utcnow()
    # a redelivery repeats receipt and timestamp, so tag the write to see if we created it
    call = os.urandom(8).hex()
    payload = json.loads(body)
    item = aws.table().update_item(
        Key={"id": event_id},
        UpdateExpression=(
            "SET #type = if_not_exists(#type, :type),"
            " payment_id = if_not_exists(payment_id, :payment_id),"
            " created_at = if_not_exists(created_at, :created_at),"
            " received_at = if_not_exists(received_at, :received_at),"
            " payload = if_not_exists(payload, :payload),"
            " #owner = if_not_exists(#owner, :receipt),"
            " #status = if_not_exists(#status, :pending),"
            " #shard = if_not_exists(#shard, :shard),"
            " attempts = if_not_exists(attempts, :no_attempts),"
            " expires_at = if_not_exists(expires_at, :expires_at),"
            " stored_by = if_not_exists(stored_by, :call)"
            " ADD receipts :receipts"
        ),
        ExpressionAttributeNames={
            "#type": "type",
            "#owner": "owner",
            "#status": "status",
            "#shard": "shard",
        },
        ExpressionAttributeValues={
            ":type": payload["type"],
            ":payment_id": payload["data"]["payment_id"],
            ":created_at": payload["timestamp"],
            ":received_at": iso(now),
            ":payload": body,
            ":receipt": receipt,
            ":pending": "pending",
            ":shard": shard_for(event_id),
            ":no_attempts": [],
            ":expires_at": int((now + KEEP_FOR).timestamp()),
            ":receipts": {receipt},
            ":call": call,
        },
        ReturnValues="ALL_NEW",
    )["Attributes"]
    return item, item["stored_by"] == call


def add_attempt(event_id: str, attempt: dict, status: str) -> None:
    """Append an attempt and set the status, but never overwrite "delivered" with a failure
    (a replay can race the worker). Keeps the newest KEEP_ATTEMPTS so replays can't push the
    item past DynamoDB's 400 KB limit."""
    table = aws.table()
    update = {
        "Key": {"id": event_id},
        "UpdateExpression": "SET attempts = list_append(attempts, :attempt), #status = :status",
        "ExpressionAttributeNames": {"#status": "status"},
        "ExpressionAttributeValues": {":attempt": [attempt], ":status": status},
        "ReturnValues": "UPDATED_NEW",
    }
    if status != "delivered":
        update["ConditionExpression"] = "#status <> :delivered"
        update["ExpressionAttributeValues"][":delivered"] = "delivered"
    try:
        attempts = table.update_item(**update)["Attributes"]["attempts"]
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        attempts = table.update_item(
            Key={"id": event_id},
            UpdateExpression="SET attempts = list_append(attempts, :attempt)",
            ExpressionAttributeValues={":attempt": [attempt]},
            ReturnValues="UPDATED_NEW",
        )["Attributes"]["attempts"]
    if len(attempts) > KEEP_ATTEMPTS:
        table.update_item(Key={"id": event_id}, UpdateExpression="REMOVE attempts[0]")


def get_event(event_id: str) -> dict | None:
    item = aws.table().get_item(Key={"id": event_id}).get("Item")
    return item if item and "payload" in item else None


def timeline(payment_id: str) -> list[dict]:
    """A payment's events in the order the sender created them.

    An event is out_of_order when it arrived before an event created ahead of it.
    """
    items = aws.table().query(
        IndexName="by_payment", KeyConditionExpression=Key("payment_id").eq(payment_id)
    )["Items"]
    arrival = {item["id"]: n for n, item in enumerate(sorted(items, key=_received), start=1)}
    latest = ""
    for item in items:
        item["arrived"] = arrival[item["id"]]
        item["out_of_order"] = item["received_at"] < latest
        latest = max(latest, item["received_at"])
    return items


def recent(
    limit: int = 50,
    *,
    event_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """The newest events first, read from every feed shard and merged."""
    filters = [
        Attr(name).eq(value) for name, value in (("type", event_type), ("status", status)) if value
    ]
    found = []
    for n in range(SHARDS):
        condition = Key("shard").eq(f"feed#{n}")
        if since:
            condition &= Key("received_at").gte(since)
        query = {
            "IndexName": "feed",
            "KeyConditionExpression": condition,
            "ScanIndexForward": False,
        }
        if filters:
            query["FilterExpression"] = functools.reduce(operator.and_, filters)
        else:
            query["Limit"] = limit
        # one page per shard, so a filtered search only sees the most recent few thousand
        found += aws.table().query(**query)["Items"]
    found.sort(key=_received, reverse=True)
    return found[:limit]


def summary(item: dict) -> dict:
    data = json.loads(item["payload"])["data"]
    row = {
        "id": item["id"],
        "type": item["type"],
        "payment_id": item["payment_id"],
        "created_at": item["created_at"],
        "received_at": item["received_at"],
        "copies_received": len(item["receipts"]),
        "status": item["status"],
        "attempts": len(item["attempts"]),
    }
    if "decline" in data:
        row["decline_code"] = data["decline"]["code"]
    for key in ("arrived", "out_of_order"):
        if key in item:
            row[key] = item[key]
    return row


def detail(item: dict) -> dict:
    return {
        **summary(item),
        "payload": json.loads(item["payload"]),
        "delivery_attempts": item["attempts"],
    }


def merchant_fixed() -> bool:
    item = aws.table().get_item(Key=MERCHANT).get("Item")
    return bool(item and item["fixed"])


def set_merchant_fixed(fixed: bool) -> None:
    aws.table().put_item(Item={**MERCHANT, "fixed": fixed})


def take_question(day: str, cap: int) -> bool:
    """Count one agent question against the day's budget. False once it's spent."""
    table = aws.table()
    try:
        table.update_item(
            Key={"id": f"quota#{day}"},
            UpdateExpression="SET expires_at = :expires_at ADD asked :one",
            ConditionExpression=Attr("asked").not_exists() | Attr("asked").lt(cap),
            ExpressionAttributeValues={
                ":one": 1,
                ":expires_at": int((utcnow() + timedelta(days=2)).timestamp()),
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def questions_asked(day: str) -> int:
    item = aws.table().get_item(Key={"id": f"quota#{day}"}).get("Item")
    return int(item["asked"]) if item else 0


def _received(item: dict) -> str:
    return item["received_at"]
