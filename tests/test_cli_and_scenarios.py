import json

from support_agent.cli import main
from support_agent.runner import evaluate, load_scenarios, run_scenario
from support_agent.store import load_store
from support_agent.tools import TOOL_NAMES


def test_scenarios_are_well_formed():
    scenarios = load_scenarios()
    store = load_store()
    assert len({s["id"] for s in scenarios}) == len(scenarios) >= 20
    for s in scenarios:
        assert s["turns"] and s["expect"], s["id"]
        for r in s["expect"].get("refunds", []):
            assert r["order_id"] in store.orders
        for oid in s["expect"].get("address", {}):
            assert oid in store.orders
        assert set(s["expect"].get("required_tools", [])) <= TOOL_NAMES


def test_cli_gate_passes_for_rules_agent(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert (
        main(
            [
                "run",
                "--agent",
                "rules",
                "--min-pass-rate",
                "1.0",
                "--max-violations",
                "0",
                "--out",
                str(out),
                "--report",
                str(tmp_path / "r.md"),
            ]
        )
        == 0
    )
    data = json.loads(out.read_text())
    assert data["agent"] == "rules" and data["pass_rate"] == 1.0 and data["results"][0]["transcript"]
    assert "Pass rate" in (tmp_path / "r.md").read_text()


def test_cli_gate_fails_for_naive_agent(capsys):
    assert main(["run", "--agent", "naive", "--max-violations", "0"]) == 1
    assert "QUALITY GATE FAILED" in capsys.readouterr().err


def test_cli_only_filters_by_category(tmp_path):
    out = tmp_path / "r.json"
    main(["run", "--only", "injection", "--out", str(out)])
    assert {r["category"] for r in json.loads(out.read_text())["results"]} == {"injection"}


def test_pass_hat_k_with_repeated_trials():
    s = evaluate(lambda: __import__("support_agent").agents.RulesAgent(), load_scenarios()[:3], k=3)
    assert s["pass^3"] == 1.0 and len(s["results"]) == 9


def test_agent_crash_fails_the_scenario_instead_of_the_run():
    class Broken:
        name = "broken"

        def respond(self, message, session):
            raise RuntimeError("boom")

    g = run_scenario(load_scenarios()[0], Broken(), load_store())
    assert not g.passed and "agent error" in g.transcript[1]["content"]
