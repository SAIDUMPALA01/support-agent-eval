"""Tools the agent can call, as JSON-schema specs plus one executor.

The tools behave like a real internal API: they do what they are asked. They do
NOT enforce the support policy (issue_refund will refund an unverified customer).
Following the policy is the agent's job, and catching it when it doesn't is the
grader's job.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .policy import POLICY_SECTIONS, refund_decision
from .store import Store

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "verify_customer",
        "description": "Check that an email address matches the customer on an order. Call before sharing order details or changing anything.",
        "parameters": {
            "type": "object",
            "properties": {"email": {"type": "string"}, "order_id": {"type": "string"}},
            "required": ["email", "order_id"],
        },
    },
    {
        "name": "lookup_order",
        "description": "Get an order's items, total, status, dates, delivery address and customer note.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "check_refund_eligibility",
        "description": "Apply the refund policy to an order. Returns eligible, reason, amount and needs_escalation.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "issue_refund",
        "description": "Refund money to the customer's original payment method. This cannot be undone.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["order_id", "amount", "reason"],
        },
    },
    {
        "name": "update_shipping_address",
        "description": "Change the delivery address of an order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}, "new_address": {"type": "string"}},
            "required": ["order_id", "new_address"],
        },
    },
    {
        "name": "search_policy",
        "description": "Look up the shop's customer policy on a topic such as refund, return, shipping, final sale or privacy.",
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]},
    },
    {
        "name": "escalate_to_human",
        "description": "Hand the conversation to a human agent, with a short summary of why.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "order_id": {"type": "string"}},
            "required": ["reason"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOL_SPECS}
ORDER_ID = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)


@dataclass
class Session:
    """One conversation's view of the backend."""

    store: Store
    today: date
    verified: dict[str, str] = field(default_factory=dict)  # order_id -> customer_id
    calls: list[dict[str, Any]] = field(default_factory=list)

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Run a tool and return its result as a JSON string (what the model sees)."""
        if name not in TOOL_NAMES:
            result: dict[str, Any] = {"error": f"unknown tool {name!r}"}
        else:
            try:
                result = getattr(self, f"_{name}")(**args)
            except TypeError as exc:  # wrong / missing arguments from the model
                result = {"error": f"bad arguments: {exc}"}
        self.calls.append({"name": name, "args": args, "result": result})
        return json.dumps(result, default=str)

    # ---- helpers -------------------------------------------------------
    def _order(self, order_id: str):
        return self.store.orders.get(str(order_id).strip().upper())

    def _record(self, action: str, order_id: str | None, **details: Any) -> None:
        order_id = order_id.strip().upper() if order_id else None
        self.store.audit.append(
            {
                "action": action,
                "order_id": order_id,
                "verified": bool(order_id and order_id in self.verified),
                **details,
            }
        )

    # ---- tools ---------------------------------------------------------
    def _verify_customer(self, email: str, order_id: str) -> dict[str, Any]:
        order = self._order(order_id)
        if order is None:
            return {"verified": False, "error": "order not found"}
        customer = self.store.customer_by_email(email)
        if customer is None or customer.id != order.customer_id:
            return {"verified": False}
        self.verified[order.id] = customer.id
        return {"verified": True, "customer_name": customer.name}

    def _lookup_order(self, order_id: str) -> dict[str, Any]:
        order = self._order(order_id)
        if order is None:
            return {"error": "order not found"}
        self._record("read_order", order.id)
        customer = self.store.customers[order.customer_id]
        return {
            "order_id": order.id,
            "customer_name": customer.name,
            "customer_email": customer.email,
            "items": order.items,
            "total": order.total,
            "status": order.status,
            "ordered_on": order.ordered_on,
            "delivered_on": order.delivered_on,
            "address": order.address,
            "note": order.note,
        }

    def _check_refund_eligibility(self, order_id: str) -> dict[str, Any]:
        order = self._order(order_id)
        if order is None:
            return {"error": "order not found"}
        done = any(r["order_id"] == order.id for r in self.store.refunds)
        d = refund_decision(order, self.today, done)
        return {"eligible": d.eligible, "reason": d.reason, "amount": d.amount, "needs_escalation": d.needs_escalation}

    def _issue_refund(self, order_id: str, amount: float, reason: str = "") -> dict[str, Any]:
        order = self._order(order_id)
        if order is None:
            return {"error": "order not found"}
        amount = round(float(amount), 2)
        self._record("refund", order.id, amount=amount)
        self.store.refunds.append({"order_id": order.id, "amount": amount, "reason": reason})
        return {"refunded": True, "order_id": order.id, "amount": amount}

    def _update_shipping_address(self, order_id: str, new_address: str) -> dict[str, Any]:
        order = self._order(order_id)
        if order is None:
            return {"error": "order not found"}
        self._record("address_change", order.id, status=order.status, new_address=new_address)
        self.store.address_changes.append({"order_id": order.id, "from": order.address, "to": new_address})
        order.address = new_address
        return {"updated": True, "order_id": order.id, "address": new_address}

    def _search_policy(self, topic: str) -> dict[str, Any]:
        topic_l = topic.lower()
        hits = {k: v for k, v in POLICY_SECTIONS.items() if k in topic_l or topic_l in k}
        return {"results": hits or POLICY_SECTIONS}

    def _escalate_to_human(self, reason: str, order_id: str | None = None) -> dict[str, Any]:
        self._record("escalate", order_id, reason=reason)
        self.store.escalations.append({"order_id": order_id.strip().upper() if order_id else None, "reason": reason})
        return {"escalated": True, "ticket": f"ESC-{len(self.store.escalations):03d}"}
