"""The ISO 8583 response codes the simulator uses, and what each means for a merchant."""

DECLINE_CODES = {
    "05": {
        "reason": "do_not_honor",
        "meaning": "The issuer declined without giving a reason.",
        "retry": False,
        "action": "Ask the customer to contact their bank or use another card.",
    },
    "14": {
        "reason": "invalid_card_number",
        "meaning": "The card number doesn't exist at the issuer.",
        "retry": False,
        "action": "Ask the customer to re-enter the card number.",
    },
    "51": {
        "reason": "insufficient_funds",
        "meaning": "The account doesn't have enough available funds.",
        "retry": True,
        "action": "Retry later, or ask the customer for another payment method.",
    },
    "54": {
        "reason": "expired_card",
        "meaning": "The card is past its expiry date.",
        "retry": False,
        "action": "Ask the customer to update the expiry date or use another card.",
    },
    "61": {
        "reason": "exceeds_withdrawal_limit",
        "meaning": "The amount is over the card's spending limit.",
        "retry": True,
        "action": "Try a smaller amount, or ask the customer to raise their limit.",
    },
    "91": {
        "reason": "issuer_unavailable",
        "meaning": "The issuer couldn't be reached to authorize the payment.",
        "retry": True,
        "action": "Retry in a few minutes; this is usually temporary.",
    },
}
