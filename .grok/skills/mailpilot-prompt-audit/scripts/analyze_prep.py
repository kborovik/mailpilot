"""Join Logfire telemetry onto the composed systems for the analysis sub-agent.

Reads ``composition.json`` (the static prompt inventory) and, when present,
``logfire_metrics.json`` (per-system telemetry the Logfire sub-agent wrote),
then computes the derived rates the analysis should reason from -- cache-read
share, tokens per invocation, tool-error rate, failed-run rate -- so the math
is deterministic and never an LLM guess. Writes ``analysis_input.json``: one
record per system with its composed prompt, its sizes, and a ``telemetry``
block (or ``null`` when the window had no activity for that system).

When ``logfire_metrics.json`` is absent (no token, or the operator skipped the
telemetry phase), the join still runs -- every ``telemetry`` block is ``null``
and the analysis degrades to a static review, noting the missing evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from _common import read_json, run_dir, write_json


def _safe_div(numerator: float, denominator: float) -> float | None:
    """Return ``numerator / denominator`` rounded, or ``None`` when undefined."""
    if not denominator:
        return None
    return round(numerator / denominator, 4)


def _derive_workflow_telemetry(raw: dict[str, Any]) -> dict[str, Any]:
    """Compute derived rates for one workflow agent's telemetry row."""
    invocations = int(raw.get("invocations", 0) or 0)
    input_tokens = int(raw.get("input_tokens_sum", 0) or 0)
    cache_read = int(raw.get("cache_read_sum", 0) or 0)
    cache_creation = int(raw.get("cache_creation_sum", 0) or 0)
    total_tokens = int(raw.get("total_tokens_sum", 0) or 0)
    tool_calls = int(raw.get("tool_call_count_sum", 0) or 0)
    tool_errors = int(raw.get("tool_error_count_sum", 0) or 0)
    status_distribution = dict(raw.get("status_distribution", {}) or {})
    failed = int(status_distribution.get("failed", 0) or 0)
    completed = int(status_distribution.get("completed", 0) or 0)
    cacheable = input_tokens + cache_read + cache_creation

    # Which composition actually ran: trigger 'task' uses the terminal-outcome
    # (TASK) protocol branch, every other trigger uses the initial-send (INITIAL)
    # branch (§V.31). The analyst needs this to know which composed prompt the
    # telemetry reflects.
    trigger_distribution = dict(raw.get("trigger_distribution", {}) or {})
    task_runs = int(trigger_distribution.get("task", 0) or 0)
    initial_runs = sum(
        int(count or 0)
        for trigger, count in trigger_distribution.items()
        if trigger != "task"
    )
    if task_runs and initial_runs:
        composition_run = "mixed"
    elif task_runs:
        composition_run = "TASK"
    elif initial_runs:
        composition_run = "INITIAL"
    else:
        composition_run = None

    return {
        "invocations": invocations,
        "model": raw.get("model"),
        "trigger_distribution": trigger_distribution,
        "composition_run": composition_run,
        "total_tokens_sum": total_tokens,
        "input_tokens_sum": input_tokens,
        "output_tokens_sum": int(raw.get("output_tokens_sum", 0) or 0),
        "cache_read_sum": cache_read,
        "cache_creation_sum": cache_creation,
        "status_distribution": status_distribution,
        "avg_prompt_length": raw.get("avg_prompt_length"),
        "avg_latency_ms": raw.get("avg_latency_ms"),
        "sample_reasonings": raw.get("sample_reasonings", []),
        "derived": {
            "tokens_per_invocation": _safe_div(total_tokens, invocations),
            "cache_read_share": _safe_div(cache_read, cacheable),
            "tool_calls_per_invocation": _safe_div(tool_calls, invocations),
            "tool_error_rate": _safe_div(tool_errors, tool_calls),
            "failed_rate": _safe_div(failed, completed + failed),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory: Path = run_dir(args.run_id)
    composition: dict[str, Any] = read_json(directory / "composition.json")

    metrics_path = directory / "logfire_metrics.json"
    metrics: dict[str, Any] = read_json(metrics_path) if metrics_path.exists() else {}
    workflow_metrics: dict[str, Any] = metrics.get("workflows", {}) or {}

    # Classifier: attach telemetry if the window captured any.
    classifier = dict(composition["classifier"])
    classifier_metrics = metrics.get("classifier")
    classifier["telemetry"] = classifier_metrics if classifier_metrics else None

    enriched_systems: list[dict[str, Any]] = []
    covered = 0
    for system in composition["workflow_systems"]:
        raw = workflow_metrics.get(system["workflow_id"])
        telemetry = _derive_workflow_telemetry(raw) if raw else None
        if telemetry is not None:
            covered += 1
        enriched_systems.append({**system, "telemetry": telemetry})

    # Order systems so the analysis spends its attention where the traffic is:
    # systems with telemetry first, by descending invocation count.
    enriched_systems.sort(
        key=lambda s: (
            s["telemetry"] is not None,
            (s["telemetry"] or {}).get("invocations", 0),
        ),
        reverse=True,
    )

    analysis_input = {
        "run_id": args.run_id,
        "window": metrics.get("window"),
        "environment": metrics.get("environment"),
        "telemetry_available": bool(metrics),
        # Spell out each derived rate so the analyst can verify it against the
        # raw sums in the telemetry block rather than guessing the denominator.
        "derived_formulas": {
            "tokens_per_invocation": "total_tokens_sum / invocations",
            "cache_read_share": (
                "cache_read_sum / "
                "(input_tokens_sum + cache_read_sum + cache_creation_sum)"
            ),
            "tool_calls_per_invocation": "tool_call_count_sum / invocations",
            "tool_error_rate": "tool_error_count_sum / tool_call_count_sum",
            "failed_rate": "failed / (completed + failed)",
        },
        "systems_covered_by_telemetry": covered,
        "systems_total": len(enriched_systems),
        "classifier": classifier,
        "template_fragments": composition["template_fragments"],
        "tool_contract": composition.get("tool_contract", {}),
        "workflow_systems": enriched_systems,
    }
    write_json(directory / "analysis_input.json", analysis_input)

    print(
        json.dumps(
            {
                "telemetry_available": bool(metrics),
                "window": metrics.get("window"),
                "systems_total": len(enriched_systems),
                "systems_covered_by_telemetry": covered,
                "top_systems": [
                    {
                        "name": s["name"],
                        "workflow_id": s["workflow_id"],
                        "invocations": (s["telemetry"] or {}).get("invocations", 0),
                    }
                    for s in enriched_systems[:5]
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
