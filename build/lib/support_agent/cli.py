"""Command line: ``support-agent-eval run`` and ``support-agent-eval chat``."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any

from .agents import AnthropicClient, LLMAgent, NaiveAgent, OpenAIClient, RulesAgent
from .agents.base import Agent
from .runner import DEFAULT_TODAY, evaluate, load_scenarios
from .store import load_store
from .tools import Session


def agent_factory(args: argparse.Namespace) -> Callable[[], Agent]:
    if args.agent == "rules":
        return RulesAgent
    if args.agent == "naive":
        return NaiveAgent
    if not args.model:
        sys.exit(f"--model is required for --agent {args.agent}")
    if args.agent == "anthropic":
        client: Any = AnthropicClient(args.model)
    else:
        client = OpenAIClient(args.model, base_url=args.base_url)
    return lambda: LLMAgent(client, name=f"{args.agent}:{args.model}")


def markdown_report(agent: str, s: dict[str, Any]) -> str:
    k = s["trials_per_scenario"]
    lines = [
        f"# Results: `{agent}`",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Scenarios | {s['scenarios']} (x{k} trials) |",
        f"| Pass rate | {s['pass_rate']:.1%} |",
        f"| pass^{k} (passed every trial) | {s[f'pass^{k}']:.1%} |",
        f"| Conversations with a policy violation | {s['conversations_with_violations']} |",
        f"| Average tool calls per conversation | {s['avg_tool_calls']} |",
        "",
        "| Category | Pass rate |",
        "|---|---|",
    ]
    lines += [f"| {c} | {r:.0%} |" for c, r in s["by_category"].items()]
    if s["violation_counts"]:
        lines += ["", "| Violation | Count |", "|---|---|"]
        lines += [f"| {v} | {n} |" for v, n in s["violation_counts"].items()]
    failed = [r for r in s["results"] if not r["passed"]]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            why = [c["name"] + (f" ({c['detail']})" if c["detail"] else "") for c in r["failed_checks"]]
            lines.append(f"- **{r['scenario_id']}** (trial {r['trial']}): " + "; ".join(why + r["violations"]))
    return "\n".join(lines) + "\n"


def cmd_run(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["id"] in wanted or s.get("category") in wanted]
    summary = evaluate(agent_factory(args), scenarios, k=args.k, store=load_store(args.store))
    name = args.agent if args.agent in ("rules", "naive") else f"{args.agent}:{args.model}"
    report = markdown_report(name, summary)
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"agent": name, **summary}, f, indent=2, default=str)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report)
    failed_gate = []
    if args.min_pass_rate is not None and summary["pass_rate"] < args.min_pass_rate:
        failed_gate.append(f"pass rate {summary['pass_rate']:.1%} < {args.min_pass_rate:.1%}")
    if args.max_violations is not None and summary["conversations_with_violations"] > args.max_violations:
        failed_gate.append(
            f"{summary['conversations_with_violations']} conversations with violations > {args.max_violations}"
        )
    for msg in failed_gate:
        print(f"QUALITY GATE FAILED: {msg}", file=sys.stderr)
    return 1 if failed_gate else 0


def cmd_chat(args: argparse.Namespace) -> int:
    agent = agent_factory(args)()
    session = Session(store=load_store(args.store), today=DEFAULT_TODAY)
    print(
        f"Chatting with the {args.agent} agent. Try: 'Refund ORD-1001, my email is priya.sharma@example.com'. "
        "Ctrl-D to quit."
    )
    while True:
        try:
            message = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not message:
            continue
        seen = len(session.calls)
        reply = agent.respond(message, session)
        for c in session.calls[seen:]:
            print(f"  [tool] {c['name']}({json.dumps(c['args'])}) -> {json.dumps(c['result'], default=str)}")
        print(f"agent> {reply}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="support-agent-eval", description="Evaluate a tool-using customer support agent.")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("run", "chat"):
        sp = sub.add_parser(name)
        sp.add_argument("--agent", choices=["rules", "naive", "anthropic", "openai"], default="rules")
        sp.add_argument("--model", help="model name for --agent anthropic/openai")
        sp.add_argument("--base-url", help="OpenAI-compatible server URL (vLLM, Ollama, ...)")
        sp.add_argument("--store", help="path to a custom store.json")
    run = sub.choices["run"]
    run.add_argument("-k", type=int, default=1, help="trials per scenario (pass^k)")
    run.add_argument("--scenarios", help="path to a custom scenarios.json")
    run.add_argument("--only", help="comma-separated scenario ids or categories")
    run.add_argument("--out", help="write full results (with transcripts) as JSON")
    run.add_argument("--report", help="write the markdown summary to a file")
    run.add_argument("--min-pass-rate", type=float, help="exit 1 if the pass rate is lower (0-1)")
    run.add_argument("--max-violations", type=int, help="exit 1 if more conversations break policy")
    args = p.parse_args(argv)
    return cmd_run(args) if args.command == "run" else cmd_chat(args)


if __name__ == "__main__":
    raise SystemExit(main())
