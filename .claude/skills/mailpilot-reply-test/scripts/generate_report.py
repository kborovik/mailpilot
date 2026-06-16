"""Assemble the brief end-of-run report (deterministic formatting, no LLM).

Merges the manifest, preflight, QA validation, per-run scoring, and (when
present) Logfire token/latency metrics into ``report.md`` and prints a short
summary to stdout. Folds in ``investigation.md`` / ``solutions.md`` when the
failure-escalation phase has produced them.

Usage:
    uv run python scripts/generate_report.py --run-id <id>
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from _common import read_json, run_dir

VERDICT_MARK = {
    "PASS": "PASS ✅",
    "FAIL": "FAIL ❌",
    "NO_REPLY": "NO_REPLY ⏱",
    "JUDGE": "JUDGE (pending)",
}


def _opt(path: Path) -> Any:
    return read_json(path) if path.exists() else None


def _delta_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round(
            (
                datetime.fromisoformat(end) - datetime.fromisoformat(start)
            ).total_seconds(),
            1,
        )
    except ValueError:
        return None


def _emit_header(
    manifest: dict, preflight: dict, validate: Any, out: list[str]
) -> None:
    out.append(f"# MailPilot reply test — report ({manifest['run_id']})")
    out.append("")
    out.append(f"- Generated: {datetime.now().astimezone().isoformat()}")
    out.append(
        f"- Workflow: `{manifest.get('workflow_id')}` (inbound-google-drive) on `{manifest['inbound_email']}`"
    )
    out.append(
        f"- Send account: `{manifest['outbound_email']}`  |  Drive KB: `{manifest['drive_folder_id']}`"
    )
    token_state = (
        "present"
        if preflight.get("logfire_ok")
        else "ABSENT (token metrics unavailable)"
    )
    out.append(
        f"- Logfire env: `{preflight.get('logfire_environment')}`  |  Logfire token: {token_state}"
    )
    if validate:
        missing = (
            f", missing {validate.get('missing')}" if validate.get("missing") else ""
        )
        out.append(
            f"- QA↔Drive validation: **{validate.get('verdict')}** "
            f"({validate.get('found_count')}/{validate.get('expected_count')} grounded{missing})"
        )
    out.append("")


def _aggregate_totals(directory: Path) -> dict[str, int]:
    totals = {"PASS": 0, "FAIL": 0, "NO_REPLY": 0}
    for run in ("A", "B"):
        scoring = _opt(directory / f"scoring_{run}.json")
        if scoring:
            for key in totals:
                totals[key] += scoring["summary"].get(key, 0)
    return totals


def _emit_run_table(
    directory: Path, run: str, metric_cases: dict, out: list[str]
) -> None:
    scoring = _opt(directory / f"scoring_{run}.json")
    if not scoring:
        return
    sends = (_opt(directory / f"sends_{run}.json") or {}).get("sends", {})
    replies = (_opt(directory / f"replies_{run}.json") or {}).get("replies", {})
    out.append(f"### Run-{run}")
    out.append("")
    out.append("| Case | Type | Verdict | Reply latency | Tokens (in/out) | Notes |")
    out.append("|---|---|---|---|---|---|")
    for case_id, graded in scoring["cases"].items():
        reply = replies.get(case_id, {})
        send = sends.get(case_id, {})
        metric = metric_cases.get(case_id, {})
        agent_ms = metric.get("latency_ms")
        if agent_ms:
            latency = f"{agent_ms / 1000:.1f}s (agent)"
        else:
            email_latency = _delta_seconds(send.get("sent_at"), reply.get("sent_at"))
            latency = f"{email_latency}s" if email_latency is not None else "—"
        toks = (
            f"{metric.get('input_tokens', '—')}/{metric.get('output_tokens', '—')}"
            if metric
            else "—"
        )
        mark = VERDICT_MARK.get(graded["verdict"], graded["verdict"])
        out.append(
            f"| `{case_id}` | {graded['type']} | {mark} | {latency} | {toks} | {_case_note(graded)} |"
        )
    out.append("")


def _emit_performance(
    directory: Path, metrics: Any, metric_cases: dict, out: list[str]
) -> None:
    out.append("## Token use & delivery performance")
    out.append("")
    if metrics and metric_cases:
        tot_in = sum(int(m.get("input_tokens", 0) or 0) for m in metric_cases.values())
        tot_out = sum(
            int(m.get("output_tokens", 0) or 0) for m in metric_cases.values()
        )
        tot_cache = sum(
            int(m.get("cache_read_input_tokens", 0) or 0) for m in metric_cases.values()
        )
        out.append(
            f"- Total tokens: **{tot_in + tot_out}** ({tot_in} in / {tot_out} out; {tot_cache} cache-read)."
        )
        lats = [
            m["latency_ms"] / 1000 for m in metric_cases.values() if m.get("latency_ms")
        ]
        if lats:
            out.append(
                f"- Agent reply latency: avg {sum(lats) / len(lats):.1f}s, max {max(lats):.1f}s (n={len(lats)})."
            )
        models = sorted(
            {str(m.get("model")) for m in metric_cases.values() if m.get("model")}
        )
        if models:
            out.append(f"- Model(s): {', '.join(models)}.")
    else:
        out.append(
            "- Logfire token/latency metrics unavailable (no token set, or analysis phase skipped). "
            "Reply latencies above are from Gmail timestamps."
        )
    _emit_concurrency(directory, out)
    out.append("")


def _emit_concurrency(directory: Path, out: list[str]) -> None:
    sends_b = _opt(directory / "sends_B.json")
    replies_b = _opt(directory / "replies_B.json")
    if not (sends_b and replies_b):
        return
    send_times = [s["sent_at"] for s in sends_b["sends"].values() if s.get("sent_at")]
    reply_times = [
        r["sent_at"] for r in replies_b["replies"].values() if r.get("sent_at")
    ]
    if not (send_times and reply_times):
        return
    wall = _delta_seconds(min(send_times), max(reply_times))
    per_case = [
        _delta_seconds(sends_b["sends"][cid].get("sent_at"), r.get("sent_at"))
        for cid, r in replies_b["replies"].items()
        if r.get("sent_at") and cid in sends_b["sends"]
    ]
    serial = round(sum(d for d in per_case if d is not None), 1)
    factor = f" (~{serial / wall:.1f}x concurrency)." if wall else "."
    out.append(
        f"- Run-B (4 concurrent): wall-clock **{wall}s**, summed per-reply {serial}s{factor}"
    )


def _cell(text: str) -> str:
    """Sanitize a note for a Markdown table cell (no pipes/newlines)."""
    return text.replace("|", "/").replace("\n", " ").strip()


def _case_note(graded: dict) -> str:
    detail = graded.get("detail", {})
    if graded["verdict"] == "PASS":
        return ""
    if graded["verdict"] == "JUDGE":
        return "awaiting judge verdict"
    # Out-scope + compare verdicts come from the Sonnet judge (§V.105); its
    # rationale is the authoritative note. In-scope keeps the missing-token note.
    rationale = detail.get("rationale")
    if rationale:
        return _cell(str(rationale))
    if graded["type"] == "inscope" and detail.get("missing"):
        return "missing: " + ", ".join(f"`{m}`" for m in detail["missing"])
    return ""


def _fold_in(path: Path, heading: str, out: list[str]) -> None:
    if path.exists():
        out.extend([heading, "", path.read_text().strip(), ""])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    directory = run_dir(args.run_id)

    manifest = read_json(directory / "run_manifest.json")
    preflight = read_json(directory / "preflight.json")
    metrics = _opt(directory / "logfire_metrics.json")
    metric_cases: dict[str, Any] = (metrics or {}).get("cases", {})

    lines: list[str] = []
    _emit_header(manifest, preflight, _opt(directory / "validate_qa.json"), lines)

    totals = _aggregate_totals(directory)
    graded_total = sum(totals.values())
    pass_rate = (
        f"{(100 * totals['PASS'] / graded_total):.0f}%" if graded_total else "n/a"
    )
    lines.extend(
        [
            "## Result",
            "",
            f"**Pass rate: {pass_rate}** — {totals['PASS']} pass, {totals['FAIL']} fail, "
            f"{totals['NO_REPLY']} no-reply (of {graded_total}).",
            "",
        ]
    )

    for run in ("A", "B"):
        _emit_run_table(directory, run, metric_cases, lines)
    _emit_performance(directory, metrics, metric_cases, lines)
    _fold_in(directory / "investigation.md", "## Failure investigation (Opus)", lines)
    _fold_in(directory / "solutions.md", "## Proposed solutions (Opus)", lines)

    report_path = directory / "report.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"Report written: {report_path}")
    print(
        f"Pass rate: {pass_rate} ({totals['PASS']} pass / {totals['FAIL']} fail / {totals['NO_REPLY']} no-reply)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
