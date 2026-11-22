from __future__ import annotations

import re
from typing import Protocol

from ..tools import ORDER_ID, Session

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
NEW_ADDRESS = re.compile(
    r"(?:address|deliver it|send it|ship it)(?:\s+(?:on|for)\s+(?:order\s+)?ORD-\d{4})?\s+(?:to|is)\s*:?\s*"
    r"(.+?)\s*(?:\.\s|\?|\.?$)",
    re.IGNORECASE,
)
MONEY = re.compile(r"£\s?(\d+(?:\.\d{1,2})?)")


class Agent(Protocol):
    """An agent sees one user message at a time and may call tools on the session."""

    name: str

    def respond(self, message: str, session: Session) -> str: ...


def parse(message: str) -> dict[str, object]:
    """Pull the order id, email, new address and intent out of a customer message.

    Used by the two rule-based agents. Deliberately simple: they exist as a
    deterministic baseline (rules) and a deliberately unsafe one (naive), so the
    evaluation can run in CI without an API key.
    """
    text = message.lower()
    order = ORDER_ID.search(message)
    email = EMAIL.search(message)
    address = NEW_ADDRESS.search(message)
    intent = None
    if any(w in text for w in ("human", "a person", "real person", "manager", "speak to someone")):
        intent = "human"
    elif address and any(w in text for w in ("change", "update", "send it", "deliver it", "ship it", "new address")):
        intent = "address"
    elif any(w in text for w in ("refund", "money back", "return it", "send it back")):
        intent = "refund"
    elif not order and any(w in text for w in ("how long", "can i return", "policy", "final sale", "returns")):
        intent = "policy"
    elif any(w in text for w in ("where is", "status", "track", "delivered")):
        intent = "status"
    # A bare order number ("It's ORD-1001") has no intent of its own, so an agent
    # keeps whatever the customer asked for earlier.
    return {
        "order_id": order.group(0).upper() if order else None,
        "email": email.group(0).rstrip(".,") if email else None,
        "new_address": address.group(1) if address and intent == "address" else None,
        "intent": intent,
        "extra_money": float(MONEY.search(message).group(1)) if MONEY.search(message) else None,
    }
