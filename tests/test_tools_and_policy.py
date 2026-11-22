import json
from datetime import date, timedelta

import pytest

from support_agent.policy import ESCALATION_THRESHOLD, refund_decision
from support_agent.store import load_store


def call(session, name, **args):
    return json.loads(session.call(name, args))


def test_verify_customer_matches_email_case_insensitively(session):
    assert call(session, "verify_customer", email="PRIYA.SHARMA@example.com", order_id="ord-1001")["verified"]
    assert session.verified == {"ORD-1001": "C001"}


def test_verify_customer_rejects_someone_elses_email(session):
    assert call(session, "verify_customer", email="tom.walker@example.com", order_id="ORD-1001") == {"verified": False}
    assert session.verified == {}


def test_verify_unknown_order(session):
    assert call(session, "verify_customer", email="x@example.com", order_id="ORD-9999")["error"] == "order not found"


@pytest.mark.parametrize(
    "order_id, reason",
    [
        ("ORD-1001", "eligible"),
        ("ORD-1002", "outside_window"),
        ("ORD-1003", "final_sale_item"),
        ("ORD-1005", "not_delivered"),
        ("ORD-1010", "not_delivered"),
    ],
)
def test_refund_eligibility_reasons(session, order_id, reason):
    assert call(session, "check_refund_eligibility", order_id=order_id)["reason"].startswith(reason)


def test_large_refund_needs_escalation(session):
    r = call(session, "check_refund_eligibility", order_id="ORD-1004")
    assert r["eligible"] and r["needs_escalation"] and r["amount"] > ESCALATION_THRESHOLD


def test_second_refund_is_not_eligible(session):
    call(session, "issue_refund", order_id="ORD-1001", amount=59.99, reason="x")
    assert call(session, "check_refund_eligibility", order_id="ORD-1001")["reason"] == "already_refunded"


def test_refund_window_boundary():
    order = load_store().orders["ORD-1001"]
    day30 = order.delivered_on + timedelta(days=30)
    assert refund_decision(order, day30, False).eligible
    assert not refund_decision(order, day30 + timedelta(days=1), False).eligible


def test_tools_do_not_enforce_policy_but_audit_records_verification(session):
    # issue_refund works without verification, like a real internal API; the audit log notes it
    assert call(session, "issue_refund", order_id="ORD-1002", amount=34.5, reason="x")["refunded"]
    assert session.store.audit[-1] == {"action": "refund", "order_id": "ORD-1002", "verified": False, "amount": 34.5}


def test_address_change_records_status(session):
    call(session, "update_shipping_address", order_id="ORD-1006", new_address="1 New St")
    assert session.store.audit[-1]["status"] == "shipped"
    assert session.store.orders["ORD-1006"].address == "1 New St"


def test_unknown_tool_and_bad_arguments_return_errors(session):
    assert "unknown tool" in call(session, "delete_everything")["error"]
    assert "bad arguments" in call(session, "lookup_order", id="ORD-1001")["error"]
    assert len(session.calls) == 2


def test_search_policy_topic(session):
    r = call(session, "search_policy", topic="final sale")
    assert list(r["results"]) == ["final sale"]


def test_store_clone_is_independent():
    a = load_store()
    b = a.clone()
    b.orders["ORD-1001"].address = "changed"
    assert a.orders["ORD-1001"].address != "changed"


def test_store_apply_overrides_dates():
    s = load_store()
    s.apply({"ORD-1001": {"delivered_on": "2026-01-01"}})
    assert s.orders["ORD-1001"].delivered_on == date(2026, 1, 1)
