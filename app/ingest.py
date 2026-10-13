"""POST /webhooks: check the signature, queue the event, answer fast."""

import json
import os

from app import aws, signing, web

MAX_BODY = 200 * 1024
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

    if not _well_formed(body):
        return web.error(400, "body must be JSON with type, timestamp and data.payment_id")

    receipt = event["requestContext"]["requestId"]
    aws.sqs().send_message(
        QueueUrl=os.environ["QUEUE_URL"],
        MessageBody=json.dumps({"id": headers["webhook-id"], "body": body, "receipt": receipt}),
    )
    return web.response(202, {"receipt": receipt})


def _well_formed(body: str) -> bool:
    # These three become DynamoDB keys, so they have to be non-empty strings.
    try:
        payload = json.loads(body)
        fields = (payload["type"], payload["timestamp"], payload["data"]["payment_id"])
    except ValueError, KeyError, TypeError:
        return False
    return all(isinstance(field, str) and field for field in fields)
