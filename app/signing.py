"""Webhook signatures as defined by Standard Webhooks (https://www.standardwebhooks.com/)."""

import base64
import hashlib
import hmac
import time

TOLERANCE = 5 * 60


class InvalidSignature(Exception):
    pass


def sign(secret: str, msg_id: str, timestamp: int, body: str) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.{body}".encode(), hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def signed_headers(
    secret: str, msg_id: str, body: str, timestamp: int | None = None
) -> dict[str, str]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": sign(secret, msg_id, timestamp, body),
        "content-type": "application/json",
    }


def verify(secret: str, headers: dict[str, str], body: str, now: float | None = None) -> None:
    """Raise InvalidSignature unless one of the signatures matches and the timestamp is fresh.

    The signature header can hold several space-separated signatures while keys rotate.
    """
    try:
        msg_id = headers["webhook-id"]
        timestamp = int(headers["webhook-timestamp"])
        signatures = headers["webhook-signature"].split()
    except (KeyError, ValueError) as error:
        raise InvalidSignature("missing or malformed webhook headers") from error

    now = time.time() if now is None else now
    if abs(now - timestamp) > TOLERANCE:
        raise InvalidSignature("timestamp is more than 5 minutes off")

    expected = sign(secret, msg_id, timestamp, body)
    if not any(hmac.compare_digest(expected, signature) for signature in signatures):
        raise InvalidSignature("no signature matches")
