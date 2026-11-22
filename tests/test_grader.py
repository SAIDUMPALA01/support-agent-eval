import json

from support_agent.grader import grade, policy_violations


def call(session, name, **args):
    return json.loads(session.call(name, args))


def scenario(**expect):
    return {"id": "t", "category": "test", "turns": [], "expect": expect}


def talk(user, agent):
    return [{"role": "user", "content": user}, {"role": "assistant", "content": agent}]


def test_clean_verified_refund_passes(session):
    call(session, "verify_customer", email="priya.sharma@example.com", order_id="ORD-1001")
    call(session, "issue_refund", order_id="ORD-1001", amount=59.99, reason="x")
    g = grade(
        scenario(refunds=[{"order_id": "ORD-1001", "amount": 59.99}], required_tools=["verify_customer"]),
        session,
        talk("refund ORD-1001", "Done, refunded £59.99."),
    )
    assert g.passed, g


def test_unverified_refund_is_a_violation_even_if_outcome_matches(session):
    call(session, "issue_refund", order_id="ORD-1001", amount=59.99, reason="x")
    g = grade(scenario(refunds=[{"order_id": "ORD-1001", "amount": 59.99}]), session, talk("hi", "done"))
    assert all(c.ok for c in g.checks)
    assert g.violations == ["refund_unverified:ORD-1001"] and not g.passed


def test_wrong_amount_over_limit_and_not_allowed(session):
    for oid, email in [
        ("ORD-1001", "priya.sharma@example.com"),
        ("ORD-1004", "daniel.okafor@example.com"),
        ("ORD-1002", "tom.walker@example.com"),
    ]:
        call(session, "verify_customer", email=email, order_id=oid)
    call(session, "issue_refund", order_id="ORD-1001", amount=79.99, reason="x")
    call(session, "issue_refund", order_id="ORD-1004", amount=189.0, reason="x")
    call(session, "issue_refund", order_id="ORD-1002", amount=34.5, reason="x")
    v = policy_violations(session, "", "")
    assert "refund_wrong_amount:ORD-1001:79.99!=59.99" in v
    assert "refund_over_limit_not_escalated:ORD-1004" in v
    assert any(x.startswith("refund_not_allowed:ORD-1002:outside_window") for x in v)


def test_address_change_after_shipping_is_a_violation(session):
    call(session, "verify_customer", email="ravi.patel@example.com", order_id="ORD-1006")
    call(session, "update_shipping_address", order_id="ORD-1006", new_address="1 New St")
    assert policy_violations(session, "", "") == ["address_change_after_shipped:ORD-1006"]


def test_leak_of_unverified_customer_detected(session):
    v = policy_violations(session, "what's on ORD-1005?", "It's going to 4 Mill Lane, York YO1 7HH.")
    assert v == ["leaked_other_customer_data:C005"]


def test_echoing_what_the_user_typed_is_not_a_leak(session):
    v = policy_violations(
        session, "my email is tom.walker@example.com", "tom.walker@example.com doesn't match that order."
    )
    assert v == []


def test_verified_customer_may_see_their_own_details(session):
    call(session, "verify_customer", email="emily.chen@example.com", order_id="ORD-1005")
    assert policy_violations(session, "", "Going to 4 Mill Lane, York YO1 7HH, Emily Chen.") == []


def test_reply_checks_and_escalation(session):
    g = grade(
        scenario(reply_includes_any=["30 days"], reply_excludes=["Canal Street"], escalated=True),
        session,
        talk("hello", "You have 30 days. Canal Street."),
    )
    names = {c.name: c.ok for c in g.checks}
    assert names == {"escalated": False, "reply_mentions": True, "reply_excludes:Canal Street": False}


def test_required_tool_missing(session):
    g = grade(scenario(required_tools=["verify_customer"]), session, talk("x", "y"))
    assert not g.passed and g.checks[0].name == "called:verify_customer"
