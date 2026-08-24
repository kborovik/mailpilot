"""Assemble the prompt-composition audit report from the run artifacts.

Reads ``analysis_input.json`` (composed systems joined with derived telemetry)
and ``analysis.md`` (the analysis subagent's findings and prioritized edits), then
writes ``report.md``: a systems-overview table, a per-system composition-sizing
table, the classifier section, the folded-in analysis, and the GitHub issues
filed for each suggested edit (when ``issues.json`` is present). Deterministic
formatting only -- every judgment lives in ``analysis.md``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from _common import read_json, run_dir


def _fmt(value: Any, suffix: str = "") -> str:
    """Render a metric cell, showing an em dash for missing values."""
    if value is None:
        return "--"
    if isinstance(value, float):
        return f"{value:g}{suffix}"
    return f"{value}{suffix}"


def _pct(value: Any) -> str:
    """Render a 0-1 ratio as a percentage cell."""
    if value is None:
        return "--"
    return f"{round(float(value) * 100, 1)}%"


def _overview_table(systems: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| System | Template | Status | Invocations | Total tokens | "
        "Cache-read share | Tool-error rate | Failed rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for system in systems:
        telemetry = system.get("telemetry")
        derived = (telemetry or {}).get("derived", {})
        lines.append(
            "| {name} | {template} | {status} | {inv} | {tok} | "
            "{cache} | {terr} | {failed} |".format(
                name=system["name"],
                template=system["template"],
                status=system["status"],
                inv=_fmt((telemetry or {}).get("invocations")),
                tok=_fmt((telemetry or {}).get("total_tokens_sum")),
                cache=_pct(derived.get("cache_read_share")),
                terr=_pct(derived.get("tool_error_rate")),
                failed=_pct(derived.get("failed_rate")),
            )
        )
    return lines


def _sizing_table(systems: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| System | Instructions (chars / ~tok) | Protocol chars | "
        "System prompt (chars) | Instruction share |",
        "|---|---|---|---|---|",
    ]
    for system in systems:
        sizes = system["sizes"]
        instructions = system["instructions"]
        lines.append(
            "| {name} | {ic} / {it} | {pc} | {spc} | {share} |".format(
                name=system["name"],
                ic=instructions["chars"],
                it=instructions["approx_tokens"],
                pc=sizes["protocol_chars"],
                spc=sizes["system_prompt_task_chars"],
                share=_pct(sizes["instruction_share"]),
            )
        )
    return lines


def _classifier_section(classifier: dict[str, Any]) -> list[str]:
    """Render the classifier section: prompt source, routing, and telemetry."""
    out = ["## Classifier", ""]
    telemetry = classifier.get("telemetry") or {}
    out.append(f"- Span: `{classifier['span']}`")
    out.append(
        f"- System prompt: {classifier['system_prompt_chars']} chars "
        f"(`{classifier['source']}`)"
    )
    out.append(f"- Routes on: {classifier['routes_on']}")
    if telemetry:
        out.append(
            f"- Invocations: {telemetry.get('invocations', '--')}; "
            f"result distribution: {telemetry.get('result_distribution', {})}"
        )
        out.append(
            f"- Tokens: {_fmt(telemetry.get('input_tokens'))} in / "
            f"{_fmt(telemetry.get('output_tokens'))} out / "
            f"{_fmt(telemetry.get('total_tokens'))} total "
            "(no cache instrumentation on this span)"
        )
        out.append(f"- Average latency: {_fmt(telemetry.get('avg_latency_ms'))} ms")
    else:
        out.append("- Telemetry: none in window")
    out.append("")
    return out


def _issues_section(payload: dict[str, Any]) -> list[str]:
    """Render the GitHub issues filed (or skipped) for this run's edits."""
    rows: list[dict[str, Any]] = payload.get("issues") or []
    out = ["## GitHub issues", ""]
    if not rows:
        out.append("No issues filed.")
        out.append("")
        return out
    out.extend(
        [
            "| Target | Repo | Issue | Status |",
            "|---|---|---|---|",
        ]
    )
    for row in rows:
        number = row.get("number")
        url = row.get("url")
        if number and url:
            issue_cell = f"[#{number}]({url})"
        elif url:
            issue_cell = url
        else:
            issue_cell = "--"
        out.append(
            "| `{target}` | {repo} | {issue} | {status} |".format(
                target=row.get("target", "--"),
                repo=row.get("repo") or "this repo",
                issue=issue_cell,
                status=row.get("status", "--"),
            )
        )
    out.append("")
    return out


def _composition_run_lines(systems: list[dict[str, Any]]) -> list[str]:
    """One line per system naming the trigger mix and the composition it ran."""
    lines: list[str] = []
    for system in systems:
        telemetry = system.get("telemetry") or {}
        run = telemetry.get("composition_run")
        if not run:
            continue
        distribution = telemetry.get("trigger_distribution") or {}
        mix = ", ".join(
            f"{trigger} {count}" for trigger, count in distribution.items() if count
        )
        lines.append(
            f"- {system['name']}: {mix or 'no triggers recorded'} -- "
            f"ran the {run} composition"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory: Path = run_dir(args.run_id)
    data: dict[str, Any] = read_json(directory / "analysis_input.json")
    systems: list[dict[str, Any]] = data["workflow_systems"]
    classifier: dict[str, Any] = data["classifier"]

    window = data.get("window")
    window_text = (
        f"{window.get('since')} to {window.get('until')}"
        if isinstance(window, dict)
        else "telemetry unavailable -- static composition only"
    )

    out: list[str] = []
    out.append("# Prompt / LLM-agent composition audit")
    out.append("")
    out.append(f"- Run: `{args.run_id}`")
    out.append(f"- Window: {window_text}")
    out.append(f"- Environment: {data.get('environment') or '--'}")
    out.append(
        f"- Systems: {data['systems_total']} workflow agents "
        f"({data['systems_covered_by_telemetry']} with telemetry) + 1 classifier"
    )
    out.append("")

    out.append("## Systems overview")
    out.append("")
    out.extend(_overview_table(systems))
    out.append("")

    out.append("## Composition sizing")
    out.append("")
    out.append(
        "Instruction share is the workflow's own TOML `instructions` as a "
        "fraction of the full composed system prompt; the remainder is the "
        "code-defined protocol (§V.44-45)."
    )
    out.append("")
    out.extend(_sizing_table(systems))
    out.append("")

    composition_lines = _composition_run_lines(systems)
    if composition_lines:
        out.append(
            "Trigger mix shows which composed prompt the window exercised: "
            "trigger `task` runs the terminal-outcome (TASK) branch, every other "
            "trigger runs the initial-send (INITIAL) branch (§V.31)."
        )
        out.append("")
        out.extend(composition_lines)
        out.append("")

    out.extend(_classifier_section(classifier))

    analysis_path = directory / "analysis.md"
    out.append("## Analysis and suggested improvements")
    out.append("")
    if analysis_path.exists():
        out.append(analysis_path.read_text().strip())
    else:
        out.append(
            "_No analysis.md present -- the analysis sub-agent did not run or "
            "did not write its findings._"
        )
    out.append("")

    issues_path = directory / "issues.json"
    if issues_path.exists():
        out.extend(_issues_section(read_json(issues_path)))

    report_path = directory / "report.md"
    report_path.write_text("\n".join(out))
    print(f"wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
