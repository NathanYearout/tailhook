"""Builds payment events in tailhook's format."""

import json
from datetime import datetime

from app.declines import DECLINE_CODES
from app.store import iso

CARDS = [("visa", "4242"), ("mastercard", "4444"), ("amex", "0005"), ("discover", "1117")]


def event_body(
    event_type: str,
    payment_id: str,
    created_at: datetime,
    *,
    amount: int = 4200,
    card: tuple[str, str] = CARDS[0],
    attempt: int = 1,
    code: str | None = None,
    note: str | None = None,
    run: str | None = None,
) -> str:
    data = {
        "payment_id": payment_id,
        "amount": amount,
        "currency": "USD",
        "card": {"brand": card[0], "last4": card[1]},
        "attempt": attempt,
    }
    if code:
        reason = DECLINE_CODES.get(code, {}).get("reason", "unknown")
        data["decline"] = {"code": code, "reason": reason}
    if note:
        data["note"] = note
    if run:
        data["run"] = run
    return json.dumps({"type": event_type, "timestamp": iso(created_at), "data": data})
