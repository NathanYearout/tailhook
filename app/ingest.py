"""POST /webhooks: check the signature, queue the event, answer fast."""

import json
import os

from app import aws, signing, web

MAX_BODY = 200 * 1024
MAX_KEY = 256  # bytes, well inside DynamoDB's key limits
REQUIRED_HEADERS = ("webhook-id", "webhook-timestamp", "webhook-signature")


def handler(event, context):
    body = web.raw_body(event)
    if len(body.encode()) > MAX_BODY:
        return web.error(413, "body is larger than 200 KB")

    headers = event.get("headers") or {}
    if not all(name in headers for name in REQUIRED_HEADERS):
        return web.error(400, "missing webhook-id, webhook-timestamp or webhook-signature")
    try:
        signing.verify(os.environ["INBOUND_SECRET"], headers, body)
    except signing.InvalidSignature as error:
        return web.error(401, str(error))

    if not _well_formed(headers["webhook-id"], body):
        return web.error(
            400, "webhook-id, type, timestamp and data.payment_id must be 1 to 256 bytes"
        )

    receipt = event["requestContext"]["requestId"]
    aws.sqs().send_message(
        QueueUrl=os.environ["QUEUE_URL"],
        MessageBody=json.dumps(
            {"id": headers["webhook-id"], "body": body, "receipt": receipt},
            ensure_ascii=False,  # escaped, non-ASCII text can triple in size
        ),
    )
    return web.response(202, {"receipt": receipt})


def _well_formed(msg_id: str, body: str) -> bool:
    # these all end up as DynamoDB keys
    try:
        payload = json.loads(body)
        fields = (msg_id, payload["type"], payload["timestamp"], payload["data"]["payment_id"])
    except (ValueError, KeyError, TypeError):
        return False
    return all(isinstance(field, str) and 0 < len(field.encode()) <= MAX_KEY for field in fields)
