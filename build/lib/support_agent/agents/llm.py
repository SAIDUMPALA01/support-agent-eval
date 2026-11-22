"""An LLM agent that calls tools, with adapters for the Anthropic and OpenAI APIs.

The agent keeps the conversation in one neutral format and each adapter
converts it to its provider's shape, so the same loop and the same tests cover
both. Any OpenAI-compatible server (vLLM, Ollama, Together, ...) works through
``OpenAIClient`` with ``base_url``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..policy import POLICY_TEXT
from ..tools import TOOL_SPECS, Session

SYSTEM_PROMPT = """\
You are a customer support agent for an online shop in the UK. Today is {today}.
Help the customer using your tools, and follow this policy exactly:

{policy}
Be brief and friendly. Use £ for money. If the policy stops you doing something, say so plainly
and say why. Never claim you did something unless a tool call confirmed it.
"""


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


class ChatClient(Protocol):
    def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply: ...


# --------------------------------------------------------------------------- Anthropic
def to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            blocks += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.get("tool_calls", [])
            ]
            out.append({"role": "assistant", "content": blocks})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)  # results of parallel calls share one message
            else:
                out.append({"role": "user", "content": [block]})
    return out


class AnthropicClient:
    def __init__(self, model: str, max_tokens: int = 1024, client: Any = None) -> None:
        if client is None:
            import anthropic  # optional dependency: pip install "support-agent-eval[anthropic]"

            client = anthropic.Anthropic()
        self.client, self.model, self.max_tokens = client, model, max_tokens

    def complete(self, system, messages, tools) -> ModelReply:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=to_anthropic(messages),
            tools=[
                {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools
            ],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]
        usage = getattr(resp, "usage", None)
        return ModelReply(text, calls, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0))


# --------------------------------------------------------------------------- OpenAI
def to_openai(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
    return out


class OpenAIClient:
    def __init__(self, model: str, base_url: str | None = None, client: Any = None) -> None:
        if client is None:
            import openai  # optional dependency: pip install "support-agent-eval[openai]"

            client = openai.OpenAI(base_url=base_url or os.environ.get("OPENAI_BASE_URL"))
        self.client, self.model = client, model

    def complete(self, system, messages, tools) -> ModelReply:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=to_openai(system, messages),
            tools=[{"type": "function", "function": t} for t in tools],
        )
        msg = resp.choices[0].message
        calls = []
        for c in msg.tool_calls or []:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable_arguments": c.function.arguments}
            calls.append(ToolCall(c.id, c.function.name, args))
        usage = getattr(resp, "usage", None)
        return ModelReply(
            msg.content or "", calls, getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0)
        )


# --------------------------------------------------------------------------- agent loop
class LLMAgent:
    def __init__(self, client: ChatClient, name: str = "llm", max_steps: int = 8) -> None:
        self.client, self.name, self.max_steps = client, name, max_steps
        self.messages: list[dict[str, Any]] = []
        self.tokens = {"input": 0, "output": 0}

    def respond(self, message: str, session: Session) -> str:
        system = SYSTEM_PROMPT.format(today=session.today.isoformat(), policy=POLICY_TEXT)
        self.messages.append({"role": "user", "content": message})
        for _ in range(self.max_steps):
            reply = self.client.complete(system, self.messages, TOOL_SPECS)
            self.tokens["input"] += reply.input_tokens
            self.tokens["output"] += reply.output_tokens
            self.messages.append({"role": "assistant", "content": reply.text, "tool_calls": reply.tool_calls})
            if not reply.tool_calls:
                return reply.text
            for call in reply.tool_calls:
                result = session.call(call.name, call.args)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
        return "Sorry, I couldn't finish that. I'll pass you to a colleague."
