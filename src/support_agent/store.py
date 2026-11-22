"""In-memory shop backend: customers, orders, refunds, escalations.

Every write goes through the tool layer, which records it in ``audit`` together
with what the agent knew at the time (was the customer verified?). The grader
reads that audit log, so it judges what the agent actually *did*, not what it
said it did.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass
class Customer:
    id: str
    name: str
    email: str


@dataclass
class Order:
    id: str
    customer_id: str
    items: list[dict[str, Any]]  # {"name": str, "price": float, "final_sale": bool}
    status: str  # processing | shipped | delivered | cancelled
    ordered_on: date
    delivered_on: date | None
    address: str
    note: str = ""  # free text from the customer; untrusted

    @property
    def total(self) -> float:
        return round(sum(i["price"] for i in self.items), 2)

    @property
    def has_final_sale_item(self) -> bool:
        return any(i.get("final_sale") for i in self.items)


@dataclass
class Store:
    customers: dict[str, Customer]
    orders: dict[str, Order]
    refunds: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    address_changes: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)

    def customer_by_email(self, email: str) -> Customer | None:
        email = email.strip().lower()
        return next((c for c in self.customers.values() if c.email.lower() == email), None)

    def clone(self) -> Store:
        return copy.deepcopy(self)

    def apply(self, changes: dict[str, dict[str, Any]]) -> None:
        """Override fields on orders for one scenario, e.g. {"ORD-1001": {"status": "shipped"}}."""
        for order_id, fields in changes.items():
            order = self.orders[order_id]
            for key, value in fields.items():
                if key in ("ordered_on", "delivered_on") and value is not None:
                    value = date.fromisoformat(value)
                setattr(order, key, value)


def load_store(path: str | None = None) -> Store:
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        raw = json.loads(resources.files("support_agent").joinpath("data/store.json").read_text("utf-8"))
    customers = {c["id"]: Customer(**c) for c in raw["customers"]}
    orders = {}
    for o in raw["orders"]:
        o = dict(o)
        o["ordered_on"] = date.fromisoformat(o["ordered_on"])
        o["delivered_on"] = date.fromisoformat(o["delivered_on"]) if o.get("delivered_on") else None
        orders[o["id"]] = Order(**o)
    return Store(customers=customers, orders=orders)
