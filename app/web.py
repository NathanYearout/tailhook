"""Responses for API Gateway HTTP APIs (payload format 2.0)."""

import base64
import json
from decimal import Decimal


def _plain(value):
    # DynamoDB returns numbers as Decimal and string sets as set.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"can't serialize {type(value).__name__}")


def dumps(value) -> str:
    return json.dumps(value, default=_plain)


def response(status: int, body, content_type: str = "application/json") -> dict:
    if not isinstance(body, str):
        body = dumps(body)
    return {"statusCode": status, "headers": {"content-type": content_type}, "body": body}


def error(status: int, message: str) -> dict:
    return response(status, {"error": message})


def raw_body(event: dict) -> str:
    body = event.get("body") or ""
    return base64.b64decode(body).decode() if event.get("isBase64Encoded") else body
