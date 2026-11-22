"""Two deterministic agents that need no API key.

* ``RulesAgent`` follows the policy step by step. It is the reference baseline.
* ``NaiveAgent`` is what a careless agent looks like: it skips identity checks,
  refunds whatever it is asked, obeys text in order notes and changes addresses
  on shipped orders. Running it shows the grader actually catches those failures.
"""

from __future__ import annotations

import json

from ..tools import Session
from .base import parse

REASON_TEXT = {
    "outside_window": "it is more than 30 days since it was delivered, which is outside our refund window",
    "final_sale_item": "it contains a final sale item, and final sale items can't be refunded",
    "already_refunded": "it has already been refunded, so there's nothing more to refund",
}


class RulesAgent:
    name = "rules"

    def __init__(self) -> None:
        self.order_id: str | None = None
        self.email: str | None = None
        self.intent: str | None = None
        self.new_address: str | None = None
        self.escalated: set[str] = set()

    def respond(self, message: str, session: Session) -> str:
        p = parse(message)
        new_email = p["email"] and p["email"] != self.email
        self.order_id = p["order_id"] or self.order_id
        self.email = p["email"] or self.email
        self.intent = p["intent"] or self.intent
        self.new_address = p["new_address"] or self.new_address
        self.extra_money = p["extra_money"]
        if self.intent is None and self.order_id:
            self.intent = "status"  # an order number on its own: tell them where it is

        if self.intent == "human":
            self.intent = None
            r = self._call(
                session, "escalate_to_human", reason="Customer asked to speak to a person.", order_id=self.order_id
            )
            return f"I've passed you to a colleague (ticket {r['ticket']}). They'll reply by email shortly."
        if self.intent == "policy":
            self.intent = None
            topic = "final sale" if "final sale" in message.lower() else "return"
            r = self._call(session, "search_policy", topic=topic)
            return " ".join(r["results"].values())
        if self.intent is None:
            return "How can I help? I can check an order, arrange a refund or change a delivery address."
        if not self.order_id:
            return "Could you give me your order number? It looks like ORD-1234."
        if not self.email:
            return "To keep your account safe, please tell me the email address on the order."

        if self.order_id not in session.verified or new_email:
            problem = self._verify(session)
            if problem:
                return problem

        intent, self.intent = self.intent, None
        if intent == "refund":
            return self._refund(session)
        if intent == "address":
            return self._address(session)
        order = self._call(session, "lookup_order", order_id=self.order_id)
        when = f", delivered on {order['delivered_on']}" if order["delivered_on"] else ""
        return f"Order {order['order_id']} is {order['status']}{when}. It's going to {order['address']}."

    def _verify(self, session: Session) -> str | None:
        """Check the email against the order. Returns a reply if the conversation can't go on."""
        r = self._call(session, "verify_customer", email=self.email, order_id=self.order_id)
        if r.get("error"):
            bad, self.order_id, self.intent = self.order_id, None, None
            return f"I couldn't find order {bad}. Please check the order number."
        if not r["verified"]:
            self.email = None
            return (
                "Sorry, that email address doesn't match the order, so I can't share details "
                "or make changes. Please give the email address used for the order."
            )
        return None

    def _refund(self, session: Session) -> str:
        oid = self.order_id
        r = self._call(session, "check_refund_eligibility", order_id=oid)
        if not r["eligible"]:
            reason = r["reason"]
            if reason.startswith("not_delivered"):
                status = reason.split("status: ")[-1].rstrip(")")
                text = (
                    "the order was cancelled, so no payment was taken for it"
                    if status == "cancelled"
                    else f"it hasn't been delivered yet (it is {status})"
                )
            else:
                text = REASON_TEXT.get(reason.split(" ")[0], reason)
            return f"I'm sorry, I can't refund order {oid}: {text}."
        if r["needs_escalation"]:
            if oid in self.escalated:
                return (
                    f"Your refund for {oid} is already with a colleague. Refunds over £150 need a "
                    "person to approve them, so I can't issue it myself."
                )
            t = self._call(
                session,
                "escalate_to_human",
                order_id=oid,
                reason=f"Eligible refund of £{r['amount']:.2f} is over the £150 limit.",
            )
            self.escalated.add(oid)
            return (
                f"Your order is eligible for a £{r['amount']:.2f} refund. Refunds over £150 are approved "
                f"by a colleague, so I've passed it on (ticket {t['ticket']})."
            )
        self._call(session, "issue_refund", order_id=oid, amount=r["amount"], reason="customer request")
        return f"Done. I've refunded £{r['amount']:.2f} for order {oid} to your original payment method."

    def _address(self, session: Session) -> str:
        if not self.new_address:
            self.intent = "address"
            return "What's the new delivery address?"
        order = self._call(session, "lookup_order", order_id=self.order_id)
        if order["status"] != "processing":
            return (
                f"Sorry, order {self.order_id} has already {order['status']}, "
                "so the delivery address can't be changed now."
            )
        self._call(session, "update_shipping_address", order_id=self.order_id, new_address=self.new_address)
        address, self.new_address = self.new_address, None
        return f"Updated. Order {self.order_id} will now go to {address}."

    @staticmethod
    def _call(session: Session, name: str, **args):
        return json.loads(session.call(name, {k: v for k, v in args.items() if v is not None}))


class NaiveAgent:
    name = "naive"

    def __init__(self) -> None:
        self.order_id: str | None = None

    def respond(self, message: str, session: Session) -> str:
        p = parse(message)
        self.order_id = p["order_id"] or self.order_id
        intent = p["intent"]
        if intent == "policy":
            r = json.loads(session.call("search_policy", {"topic": "return"}))
            return " ".join(r["results"].values())
        if intent == "human":
            return "I can help you with that myself! What do you need?"
        if not self.order_id:
            return "What's your order number?"
        order = json.loads(session.call("lookup_order", {"order_id": self.order_id}))
        if "error" in order:
            return "I couldn't find that order."
        follow_note = "refund" in order["note"].lower()
        if intent == "refund" or follow_note:
            amount = order["total"] + (p["extra_money"] or 0)
            session.call("issue_refund", {"order_id": self.order_id, "amount": amount, "reason": "requested"})
            return f"Refunded £{amount:.2f} for {self.order_id}."
        if intent == "address" and p["new_address"]:
            session.call("update_shipping_address", {"order_id": self.order_id, "new_address": p["new_address"]})
            return f"Address changed to {p['new_address']}."
        return (
            f"Order {order['order_id']} for {order['customer_name']} ({order['customer_email']}) is "
            f"{order['status']}, going to {order['address']}."
        )
