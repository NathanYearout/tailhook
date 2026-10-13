import base64
import json
from decimal import Decimal

from app import web


def test_dumps_handles_dynamodb_types():
    value = {"n": Decimal("3"), "f": Decimal("1.5"), "s": {"b", "a"}}
    assert json.loads(web.dumps(value)) == {"n": 3, "f": 1.5, "s": ["a", "b"]}


def test_response_and_error():
    assert web.response(201, {"ok": True}) == {
        "statusCode": 201,
        "headers": {"content-type": "application/json"},
        "body": '{"ok": true}',
    }
    assert web.response(200, "hi", "text/plain")["body"] == "hi"
    assert web.error(404, "nope")["body"] == '{"error": "nope"}'


def test_raw_body_decodes_base64():
    encoded = base64.b64encode("café".encode()).decode()
    assert web.raw_body({"body": encoded, "isBase64Encoded": True}) == "café"
    assert web.raw_body({"body": None}) == ""
