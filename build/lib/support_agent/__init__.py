"""support-agent-eval: a tool-using customer support agent and the tests that keep it honest."""

from .grader import Grade, grade
from .runner import evaluate, load_scenarios, run_scenario
from .store import load_store
from .tools import TOOL_SPECS, Session

__version__ = "0.1.0"
__all__ = [
    "TOOL_SPECS",
    "Grade",
    "Session",
    "evaluate",
    "grade",
    "load_scenarios",
    "load_store",
    "run_scenario",
]
