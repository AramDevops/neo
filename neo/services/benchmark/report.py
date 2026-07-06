"""Aggregate scenario results into the benchmark summary and render it."""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List

from ...config import ARTIFACTS_DIR


def summarize(results: List[Dict[str, Any]], total_wall_ms: int) -> Dict[str, Any]:
    latencies = [r["latency_ms"] for r in results] or [0]
    expected_blocked = [r for r in results if r["expected_status"] == "blocked"]
    categories: Dict[str, Dict[str, int]] = {}
    for r in results:
        bucket = categories.setdefault(r["category"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r["passed"] else 0

    return {
        "benchmark": "neo-harness-v1",
        "scenarios": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "pass_rate": round(100.0 * sum(1 for r in results if r["passed"]) / max(len(results), 1), 1),
        "false_success_count": sum(1 for r in results if r["false_success"]),
        "false_success_rate": round(100.0 * sum(1 for r in results if r["false_success"]) / max(len(expected_blocked), 1), 1) if expected_blocked else 0.0,
        "verdict_consistency_rate": round(100.0 * sum(1 for r in results if r["verdict_consistent"]) / max(len(results), 1), 1),
        "recovered_scenarios": sum(1 for r in results if r["recovered"]),
        "latency_ms": {
            "mean": int(statistics.mean(latencies)),
            "median": int(statistics.median(latencies)),
            "max": max(latencies),
        },
        "total_wall_ms": total_wall_ms,
        "tool_calls_total": sum(r["tool_calls"] for r in results),
        "tool_failures_total": sum(r["tool_failures"] for r in results),
        "policy_retries_total": sum(r["policy_retries"] for r in results),
        "categories": categories,
        "results": results,
    }


def write_artifact(summary: Dict[str, Any], artifact_dir: Path | None = None) -> Path:
    out_dir = Path(artifact_dir) if artifact_dir else ARTIFACTS_DIR / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    artifact_path = out_dir / f"benchmark_{stamp}.json"
    artifact_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return artifact_path


def print_summary(summary: Dict[str, Any]) -> None:
    header = f"{'scenario':<26} {'category':<13} {'expect':<8} {'actual':<8} {'pass':<5} {'ms':>7} {'loops':>5} {'tools':>5} {'fails':>5} {'retry':>5}"
    print(header)
    print("-" * len(header))
    for r in summary["results"]:
        print(
            f"{r['id']:<26} {r['category']:<13} {r['expected_status']:<8} {r['status']:<8} "
            f"{'PASS' if r['passed'] else 'FAIL':<5} {r['latency_ms']:>7} {r['loops']:>5} "
            f"{r['tool_calls']:>5} {r['tool_failures']:>5} {r['policy_retries']:>5}"
        )
    print("-" * len(header))
    print(
        f"pass rate {summary['pass_rate']}% ({summary['passed']}/{summary['scenarios']}) | "
        f"false-success {summary['false_success_count']} | "
        f"verdict consistency {summary['verdict_consistency_rate']}% | "
        f"recovered {summary['recovered_scenarios']} | "
        f"latency mean {summary['latency_ms']['mean']}ms median {summary['latency_ms']['median']}ms max {summary['latency_ms']['max']}ms | "
        f"wall {summary['total_wall_ms']}ms"
    )
    print(f"artifact: {summary['artifact_path']}")
