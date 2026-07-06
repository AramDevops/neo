"""Compare models on the eval suite and draw performance/quality charts.

Runs the same eval suite across several models through the real harness, then
renders one colorblind-safe comparison dashboard (quality and speed side by
side) plus a small markdown summary. Real runs need a provider key; use
`--from-db` to re-plot the latest stored eval per model without re-running, or
`--demo` to render illustrative sample numbers so you can see the format.

    python -m neo.services.model_compare --models gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro
    python -m neo.services.model_compare --from-db --models gemini-2.5-flash,gemini-2.5-pro
    python -m neo.services.model_compare --demo
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from ..config import ARTIFACTS_DIR
from ..db import Database
from .evals import is_transport_error


# Okabe-Ito, a colorblind-safe categorical palette. Models are assigned a color
# in the order given, never cycled or reassigned.
_PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442"]
_INK = "#1a1d21"
_MUTED = "#6b7280"
_GRID = "#e5e7eb"
_SURFACE = "#ffffff"


@dataclass
class ModelResult:
    label: str
    pass_rate: float                       # 0-100, over graded (non-errored) tasks
    passed: int
    total: int                             # graded tasks (transport errors excluded)
    avg_latency_ms: float
    avg_tokens: float
    category_pass: Dict[str, float] = field(default_factory=dict)  # category -> 0-100
    errored: int = 0                       # tasks dropped for network/provider errors


def _is_errored(item: Dict) -> bool:
    # Fresh evals stamp the item; older evals only have the run's raw error, so fall
    # back to the linked run's transport signature (status/loops/tools/error text).
    raw = item.get("details_json") or ""
    if '"errored": true' in raw or '"errored":true' in raw:
        return True
    return is_transport_error(item)


def _result_from_eval(db: Database, eval_id: int) -> ModelResult:
    run = db.fetchone("SELECT model, score, passed, total FROM eval_runs WHERE id=?", (eval_id,)) or {}
    items = db.fetchall(
        "SELECT ei.category, ei.passed, ei.latency_ms, ei.details_json, "
        "r.token_estimate, r.status, r.error_text, r.loop_count, r.tool_count "
        "FROM eval_items ei LEFT JOIN runs r ON r.id=ei.run_id WHERE ei.eval_run_id=?",
        (eval_id,),
    )
    # A transport error (DNS/socket/rate-limit) is not the model's fault: keep those
    # runs out of the rates and latencies so the chart measures the model, not the wifi.
    # Derive the rate from the graded items (not the stored eval_runs.score) so re-
    # plotting an eval saved before this fix reflects the corrected methodology too.
    errored = sum(1 for i in items if _is_errored(i))
    graded = [i for i in items if not _is_errored(i)]
    total = len(graded)
    passed = sum(1 for i in graded if i.get("passed"))
    pass_rate = round(100 * passed / total, 1) if total else 0.0
    latencies = [float(i.get("latency_ms") or 0) for i in graded]
    tokens = [float(i.get("token_estimate") or 0) for i in graded if i.get("token_estimate")]
    by_cat: Dict[str, List[int]] = {}
    for item in graded:
        by_cat.setdefault(str(item.get("category") or "other"), []).append(1 if item.get("passed") else 0)
    category_pass = {cat: round(100 * sum(vals) / len(vals), 1) for cat, vals in by_cat.items() if vals}
    return ModelResult(
        label=str(run.get("model") or f"eval {eval_id}"),
        pass_rate=pass_rate,
        passed=passed,
        total=total,
        avg_latency_ms=round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        avg_tokens=round(sum(tokens) / len(tokens), 1) if tokens else 0.0,
        category_pass=category_pass,
        errored=errored,
    )


def run_models(models: List[str], provider: str = "gemini", db: Database | None = None) -> List[ModelResult]:
    """Run the eval suite for each model (real provider calls), newest first."""
    from .evals import EvalService

    db = db or Database()
    service = EvalService(db)
    results: List[ModelResult] = []
    for model in models:
        eval_id = service.run_eval(provider, model)
        results.append(_result_from_eval(db, eval_id))
    return results


def collect_latest_from_db(models: List[str], db: Database | None = None) -> List[ModelResult]:
    """Re-plot the most recent completed eval for each model, without running."""
    db = db or Database()
    results: List[ModelResult] = []
    for model in models:
        row = db.fetchone(
            "SELECT id FROM eval_runs WHERE model=? AND status='complete' ORDER BY id DESC LIMIT 1",
            (model,),
        )
        if row:
            results.append(_result_from_eval(db, int(row["id"])))
    return results


def render_report(results: List[ModelResult], out_dir: Path, demo: bool = False) -> Dict[str, str]:
    """Draw one comparison dashboard (PNG) and a markdown summary. Returns paths."""
    import matplotlib
    matplotlib.use("Agg")  # headless: write files, never open a window
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)
    labels = [r.label for r in results]
    colors = [_PALETTE[i % len(_PALETTE)] for i in range(len(results))]

    fig, axes = plt.subplots(2, 2, figsize=(12, 8.5))
    fig.patch.set_facecolor(_SURFACE)
    title = "Neo model comparison" + ("  (illustrative sample)" if demo else "")
    fig.suptitle(title, fontsize=15, fontweight="bold", color=_INK, x=0.02, ha="left")

    for ax in axes.flat:
        ax.set_facecolor(_SURFACE)
        for spine in ("top", "right"):
            ax.spines[spine].set_visible(False)
        for spine in ("left", "bottom"):
            ax.spines[spine].set_color(_GRID)
        ax.tick_params(colors=_MUTED, labelsize=9)

    # 1. Quality vs speed (the headline): top-left is fast and accurate.
    ax = axes[0][0]
    for r, c in zip(results, colors):
        ax.scatter(r.avg_latency_ms / 1000, r.pass_rate, s=180, color=c, zorder=3, edgecolor="white", linewidth=1.5)
        ax.annotate(r.label, (r.avg_latency_ms / 1000, r.pass_rate), textcoords="offset points",
                    xytext=(8, 6), fontsize=9, color=_INK)
    ax.set_title("Quality vs speed", fontsize=11, color=_INK, loc="left", fontweight="bold")
    ax.set_xlabel("avg latency per task (s)", fontsize=9, color=_MUTED)
    ax.set_ylabel("pass rate (%)", fontsize=9, color=_MUTED)
    ax.set_ylim(0, 105)
    ax.grid(True, color=_GRID, linewidth=0.8, zorder=0)

    # 2. Pass rate per model (magnitude): direct-labeled bars.
    ax = axes[0][1]
    bars = ax.bar(labels, [r.pass_rate for r in results], color=colors, width=0.6, zorder=3)
    for bar, r in zip(bars, results):
        ax.text(bar.get_x() + bar.get_width() / 2, r.pass_rate + 1.5, f"{r.pass_rate:.0f}%\n{r.passed}/{r.total}",
                ha="center", va="bottom", fontsize=9, color=_INK)
    ax.set_title("Pass rate", fontsize=11, color=_INK, loc="left", fontweight="bold")
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.grid(True, axis="y", color=_GRID, linewidth=0.8, zorder=0)
    ax.tick_params(axis="x", labelrotation=15)

    # 3. Pass rate by category (grouped bars; model = fixed categorical color).
    ax = axes[1][0]
    cats = sorted({c for r in results for c in r.category_pass})
    n = max(len(results), 1)
    width = 0.8 / n
    for idx, (r, c) in enumerate(zip(results, colors)):
        xs = [j + (idx - (n - 1) / 2) * width for j in range(len(cats))]
        ax.bar(xs, [r.category_pass.get(cat, 0) for cat in cats], width=width, color=c, label=r.label, zorder=3)
    ax.set_title("Pass rate by category", fontsize=11, color=_INK, loc="left", fontweight="bold")
    ax.set_xticks(range(len(cats)))
    ax.set_xticklabels(cats, fontsize=9, rotation=15)
    ax.set_ylim(0, 112)
    ax.set_yticks([0, 50, 100])
    ax.grid(True, axis="y", color=_GRID, linewidth=0.8, zorder=0)
    if len(results) >= 2:
        ax.legend(frameon=False, fontsize=8, loc="lower right", ncol=len(results))

    # 4. Speed and cost per task (avg latency; token estimate as a second small view).
    ax = axes[1][1]
    bars = ax.bar(labels, [r.avg_latency_ms / 1000 for r in results], color=colors, width=0.6, zorder=3)
    for bar, r in zip(bars, results):
        note = f"{r.avg_latency_ms/1000:.1f}s"
        if r.avg_tokens:
            note += f"\n~{int(r.avg_tokens)} tok"
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), note,
                ha="center", va="bottom", fontsize=9, color=_INK)
    ax.set_title("Avg latency per task", fontsize=11, color=_INK, loc="left", fontweight="bold")
    ax.set_ylabel("seconds", fontsize=9, color=_MUTED)
    ax.grid(True, axis="y", color=_GRID, linewidth=0.8, zorder=0)
    ax.tick_params(axis="x", labelrotation=15)
    top = max((r.avg_latency_ms / 1000 for r in results), default=1)
    ax.set_ylim(0, top * 1.25)

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    png = out_dir / "model_comparison.png"
    fig.savefig(png, dpi=140, facecolor=_SURFACE)
    plt.close(fig)

    # Markdown summary next to the image.
    lines = ["# Model comparison", "", "| Model | Pass rate | Avg latency | Avg tokens | Excluded (network) |", "|---|---|---|---|---|"]
    for r in results:
        tok = f"~{int(r.avg_tokens)}" if r.avg_tokens else "-"
        exc = str(r.errored) if r.errored else "-"
        lines.append(f"| {r.label} | {r.pass_rate:.0f}% ({r.passed}/{r.total}) | {r.avg_latency_ms/1000:.1f}s | {tok} | {exc} |")
    if any(r.errored for r in results):
        lines += ["", "Excluded runs died on transport errors (DNS/socket/rate-limit) before the "
                  "model answered; they are not graded as failures."]
    lines += ["", "![comparison](model_comparison.png)"]
    md = out_dir / "model_comparison.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"png": str(png), "md": str(md)}


def _demo_results() -> List[ModelResult]:
    """Illustrative numbers (no API calls) so the chart format is reviewable.

    Shaped like a real run: flash and the flagship pro both ace the tasks, but
    flash does it far faster and cheaper, while the cheapest lite loops and burns
    tokens on about half. The real chart is docs/model_comparison.png.
    """
    return [
        ModelResult("gemini-2.5-flash-lite", 45.5, 5, 11, 18500, 6000,
                    {"logic": 100, "programming": 0, "operations": 0, "safety": 100, "tools": 42.9}),
        ModelResult("gemini-2.5-flash", 100.0, 11, 11, 12800, 2200,
                    {"logic": 100, "programming": 100, "operations": 100, "safety": 100, "tools": 100}),
        ModelResult("gemini-2.5-pro", 100.0, 11, 11, 29000, 3200,
                    {"logic": 100, "programming": 100, "operations": 100, "safety": 100, "tools": 100}),
    ]


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare models on the eval suite and chart the results.")
    parser.add_argument("--models", default="", help="comma-separated model ids, e.g. gemini-2.5-flash-lite,gemini-2.5-flash,gemini-2.5-pro")
    parser.add_argument("--provider", default="gemini")
    parser.add_argument("--from-db", action="store_true", help="re-plot the latest stored eval per model instead of running")
    parser.add_argument("--demo", action="store_true", help="render illustrative sample numbers (no API calls)")
    parser.add_argument("--out", default=str(ARTIFACTS_DIR / "reports"))
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    if args.demo:
        results = _demo_results()
    else:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        if not models:
            parser.error("pass --models a,b,c (or use --demo)")
        results = collect_latest_from_db(models) if args.from_db else run_models(models, args.provider)
    if not results:
        print("No results to plot. Run an eval first, or use --demo.")
        return 1

    paths = render_report(results, out_dir, demo=args.demo)
    print("Wrote:")
    print(" ", paths["png"])
    print(" ", paths["md"])
    for r in results:
        print(f"  {r.label:28} {r.pass_rate:5.1f}%  {r.avg_latency_ms/1000:5.1f}s/task")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
