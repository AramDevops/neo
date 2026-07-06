"""Command-line entry point: python -m neo.services.benchmark."""

from __future__ import annotations

import argparse
from typing import List

from .report import print_summary
from .runner import run_benchmark
from .scenarios import SCENARIOS


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Neo harness benchmark.")
    parser.add_argument("--only", help="Comma-separated scenario ids to run.")
    parser.add_argument("--list", action="store_true", help="List scenario ids and exit.")
    args = parser.parse_args(argv)
    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.id:<26} [{scenario.category}] expect={scenario.expected_status} :: {scenario.prompt}")
        return 0
    only = [item.strip() for item in args.only.split(",") if item.strip()] if args.only else None
    summary = run_benchmark(only=only)
    print_summary(summary)
    return 0 if summary["passed"] == summary["scenarios"] else 1
