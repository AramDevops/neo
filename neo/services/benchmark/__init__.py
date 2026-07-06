"""Harness benchmark: real scenarios, scripted model, evidence-based grading.

Each scenario runs through the full production stack (AgentRunner, verdict
engine, runtime controller, Toolbox, ProcessManager) inside an isolated
workspace and sqlite database. The model is a deterministic script, so every
metric measures the HARNESS: does it refuse false success, recover from real
failures, verify before claiming done, and keep run status / plan state /
final text consistent?

Grading reads database state (run status, plan rows, tool events, verdict
events) and the real filesystem, never just the model's words.

Package layout:
- harness.py    scripted provider, simulated toolbox, the Scenario type
- scenarios/    scenario definitions, one module per harness concern
- runner.py     configures isolation and executes scenarios for grading
- report.py     summary aggregation, artifact writing, console table
- cli.py        argument parsing for the module entry point

Run it with:  python -m neo.services.benchmark
"""

from .cli import main
from .harness import Scenario, ScriptedProvider, SimulatedBrowserToolbox, free_port, observed, setup_noop
from .report import print_summary, summarize, write_artifact
from .runner import configure, run_benchmark, run_scenario
from .scenarios import SCENARIOS

__all__ = [
    "SCENARIOS",
    "Scenario",
    "ScriptedProvider",
    "SimulatedBrowserToolbox",
    "configure",
    "free_port",
    "main",
    "observed",
    "print_summary",
    "run_benchmark",
    "run_scenario",
    "setup_noop",
    "summarize",
    "write_artifact",
]
