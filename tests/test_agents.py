import json
from types import SimpleNamespace

import pytest

from support_agent.agents import AnthropicClient, LLMAgent, NaiveAgent, OpenAIClient, RulesAgent
from support_agent.agents.base import parse
from support_agent.agents.llm import ModelReply, ToolCall, to_anthropic, to_openai
from support_agent.runner import evaluate, load_scenarios


# ---------------------------------------------------------------- offline agents
def test_rules_agent_passes_every_scenario_with_no_violations():
    s = evaluate(RulesAgent, load_scenarios())
    failed = [r["scenario_id"] for r in s["results"] if not r["passed"]]
    assert failed == [] and s["conversations_with_violations"] == 0


def test_naive_agent_is_caught_by_the_grader():
    s = evaluate(NaiveAgent, load_scenarios())
    assert s["pass_rate"] < 0.2
    for kind in (
        "refund_unverified",
        "refund_not_allowed",
        "leaked_other_customer_data",
        "refund_over_limit_not_escalated",
        "refund_wrong_amount",
    ):
        assert s["violation_counts"].get(kind, 0) > 0, kind


def test_parse_extracts_ids_email_and_address():
    p = parse("Please change the delivery address on ord-1005 to 22 Park Road, Leeds LS6 2AB. Email a.b@example.com")
    assert p["order_id"] == "ORD-1005" and p["email"] == "a.b@example.com"
    assert p["intent"] == "address" and p["new_address"] == "22 Park Road, Leeds LS6 2AB"


@pytest.mark.parametrize(
    "text, intent",
    [
        ("I want to speak to a real person", "human"),
        ("refund ORD-1001", "refund"),
        ("How long do I have to return something?", "policy"),
        ("Where is ORD-1006?", "status"),
        ("hello", None),
    ],
)
def test_parse_intents(text, intent):
    assert parse(text)["intent"] == intent


def test_rules_agent_asks_for_missing_details(session):
    agent = RulesAgent()
    assert "order number" in agent.respond("I want a refund", session)
    assert "email" in agent.respond("It's ORD-1001", session)
    assert "refunded £59.99" in agent.respond("priya.sharma@example.com", session)


# ---------------------------------------------------------------- LLM agent loop
class ScriptedClient:
    """Returns pre-written replies and records what it was sent."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, system, messages, tools):
        self.seen.append({"system": system, "messages": [dict(m) for m in messages], "tools": tools})
        return self.replies.pop(0)


def test_llm_agent_runs_tool_calls_then_answers(session):
    client = ScriptedClient(
        [
            ModelReply(
                "", [ToolCall("t1", "verify_customer", {"email": "priya.sharma@example.com", "order_id": "ORD-1001"})]
            ),
            ModelReply(
                "",
                [
                    ToolCall("t2", "check_refund_eligibility", {"order_id": "ORD-1001"}),
                    ToolCall("t3", "issue_refund", {"order_id": "ORD-1001", "amount": 59.99, "reason": "r"}),
                ],
                input_tokens=10,
                output_tokens=5,
            ),
            ModelReply("Refunded £59.99."),
        ]
    )
    agent = LLMAgent(client)
    assert agent.respond("refund ORD-1001, priya.sharma@example.com", session) == "Refunded £59.99."
    assert [c["name"] for c in session.calls] == ["verify_customer", "check_refund_eligibility", "issue_refund"]
    assert session.store.refunds == [{"order_id": "ORD-1001", "amount": 59.99, "reason": "r"}]
    assert "2026-11-20" in client.seen[0]["system"] and "verify_customer" in client.seen[0]["system"]
    last = client.seen[-1]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "tool", "assistant", "tool", "tool"]
    assert json.loads(last[2]["content"])["verified"] is True
    assert agent.tokens == {"input": 10, "output": 5}


def test_llm_agent_stops_after_max_steps(session):
    loop = [ModelReply("", [ToolCall(f"t{i}", "search_policy", {"topic": "refund"})]) for i in range(3)]
    agent = LLMAgent(ScriptedClient(loop), max_steps=3)
    assert "colleague" in agent.respond("hi", session)
    assert len(session.calls) == 3


def test_llm_agent_keeps_history_across_turns(session):
    client = ScriptedClient([ModelReply("Which order?"), ModelReply("Thanks.")])
    agent = LLMAgent(client)
    agent.respond("I need help", session)
    agent.respond("ORD-1001", session)
    assert [m["content"] for m in client.seen[1]["messages"] if m["role"] == "user"] == ["I need help", "ORD-1001"]


# ---------------------------------------------------------------- provider adapters
HISTORY = [
    {"role": "user", "content": "refund ORD-1001"},
    {
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [
            ToolCall("a", "lookup_order", {"order_id": "ORD-1001"}),
            ToolCall("b", "search_policy", {"topic": "refund"}),
        ],
    },
    {"role": "tool", "tool_call_id": "a", "name": "lookup_order", "content": "{}"},
    {"role": "tool", "tool_call_id": "b", "name": "search_policy", "content": "{}"},
]


def test_to_anthropic_groups_parallel_tool_results_in_one_user_message():
    out = to_anthropic(HISTORY)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]


def test_to_openai_uses_tool_messages_and_json_arguments():
    out = to_openai("SYS", HISTORY)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"order_id": "ORD-1001"}
    assert [m["role"] for m in out[3:]] == ["tool", "tool"]


def test_anthropic_client_parses_text_and_tool_use():
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Let me check."),
            SimpleNamespace(type="tool_use", id="x", name="lookup_order", input={"order_id": "ORD-1001"}),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )
    sent = {}
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: sent.update(kw) or resp))
    reply = AnthropicClient("some-model", client=fake).complete(
        "SYS", [{"role": "user", "content": "hi"}], [{"name": "t", "description": "d", "parameters": {}}]
    )
    assert reply.text == "Let me check." and reply.tool_calls[0].args == {"order_id": "ORD-1001"}
    assert reply.input_tokens == 12 and sent["tools"][0]["input_schema"] == {} and sent["system"] == "SYS"


def test_openai_client_survives_malformed_tool_arguments():
    call = SimpleNamespace(id="1", function=SimpleNamespace(name="lookup_order", arguments="{not json"))
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))], usage=None
    )
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: resp)))
    reply = OpenAIClient("m", client=fake).complete("S", [], [])
    assert reply.text == "" and reply.tool_calls[0].args == {"_unparseable_arguments": "{not json"}
