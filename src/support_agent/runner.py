"""Run scenarios against an agent, k times each, and aggregate the grades."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from .agents.base import Agent
from .grader import Grade, grade
from .store import Store, load_store
from .tools import Session

DEFAULT_TODAY = date(2026, 11, 20)


def load_scenarios(path: str | None = None) -> list[dict[str, Any]]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    return json.loads(resources.files("support_agent").joinpath("data/scenarios.json").read_text("utf-8"))


def run_scenario(scenario: dict[str, Any], agent: Agent, base: Store) -> Grade:
    store = base.clone()
    store.apply(scenario.get("setup", {}))
    today = date.fromisoformat(scenario["today"]) if scenario.get("today") else DEFAULT_TODAY
    session = Session(store=store, today=today)
    transcript: list[dict[str, str]] = []
    for turn in scenario["turns"]:
        transcript.append({"role": "user", "content": turn})
        try:
            reply = agent.respond(turn, session)
        except Exception as exc:  # noqa: BLE001 - an agent crash fails the scenario, it doesn't stop the run
            reply = f"[agent error: {type(exc).__name__}: {exc}]"
        transcript.append({"role": "assistant", "content": reply})
    return grade(scenario, session, transcript)


def evaluate(
    make_agent: Callable[[], Agent], scenarios: list[dict[str, Any]], k: int = 1, store: Store | None = None
) -> dict[str, Any]:
    base = store or load_store()
    started = time.perf_counter()
    trials: dict[str, list[Grade]] = defaultdict(list)
    for _ in range(k):
        for s in scenarios:
            trials[s["id"]].append(run_scenario(s, make_agent(), base))
    return summarise(trials, k, time.perf_counter() - started)


def summarise(trials: dict[str, list[Grade]], k: int, seconds: float) -> dict[str, Any]:
    grades = [g for gs in trials.values() for g in gs]
    n = len(grades)
    by_cat: dict[str, list[Grade]] = defaultdict(list)
    for g in grades:
        by_cat[g.category].append(g)
    violations: dict[str, int] = defaultdict(int)
    for g in grades:
        for v in g.violations:
            violations[v.split(":")[0]] += 1
    return {
        "scenarios": len(trials),
        "trials_per_scenario": k,
        "pass_rate": round(sum(g.passed for g in grades) / n, 4) if n else 0.0,
        f"pass^{k}": round(sum(all(g.passed for g in gs) for gs in trials.values()) / len(trials), 4)
        if trials
        else 0.0,
        "conversations_with_violations": sum(bool(g.violations) for g in grades),
        "violation_counts": dict(sorted(violations.items())),
        "avg_tool_calls": round(sum(len(g.tool_calls) for g in grades) / n, 2) if n else 0.0,
        "by_category": {c: round(sum(g.passed for g in gs) / len(gs), 4) for c, gs in sorted(by_cat.items())},
        "seconds": round(seconds, 2),
        "results": [
            {
                "scenario_id": g.scenario_id,
                "category": g.category,
                "trial": i + 1,
                "passed": g.passed,
                "failed_checks": [asdict(c) for c in g.checks if not c.ok],
                "violations": g.violations,
                "tool_calls": g.tool_calls,
                "transcript": g.transcript,
            }
            for gs in trials.values()
            for i, g in enumerate(gs)
        ],
    }
