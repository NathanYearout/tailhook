"""Sends an event to the destination app and records how it went."""

import os
import time
import urllib.error
import urllib.request

from app import signing, store

TIMEOUT = 5
SNIPPET = 300


def deliver(item: dict, *, failed_status: str, replay: bool = False) -> bool:
    """POST the event to DESTINATION_URL and record the attempt on the event.

    The body is re-signed with a fresh timestamp but keeps its webhook-id, so the app can
    still dedupe, and a replay never fails the app's timestamp check.
    """
    body = item["payload"]
    request = urllib.request.Request(
        os.environ["DESTINATION_URL"],
        data=body.encode(),
        headers=signing.signed_headers(os.environ["OUTBOUND_SECRET"], item["id"], body),
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            status_code, reply = response.status, response.read(SNIPPET)
    except urllib.error.HTTPError as error:
        status_code, reply = error.code, error.read(SNIPPET)
    except OSError as error:  # refused, DNS failure, timeout
        status_code, reply = 0, str(error).encode()

    ok = 200 <= status_code < 300
    attempt = {
        "at": store.iso(store.utcnow()),
        "status_code": status_code,
        "ms": round((time.monotonic() - started) * 1000),
        "body": reply.decode(errors="replace"),
        "replay": replay,
    }
    store.add_attempt(item["id"], attempt, "delivered" if ok else failed_status)
    return ok
