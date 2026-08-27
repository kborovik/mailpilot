"""Fold every phase into one Markdown report and an overall verdict.

Joins the Touch 1 (send), reply-injection, handling, and branch-verification
artifacts into a per-scenario table plus a PASS/FAIL verdict. PASS requires every
scenario's observed branch to match its expectation and no setup/send/route
failure along the way. The workflow-wording critique is advisory: it adds a
score line and a section suggesting edits to the workflow's reply handling, but
never changes the verdict. Writes ``report.md`` and prints it.

Usage:
    uv run python scripts/generate_report.py --run-id <id>
"""

from __future__ import annotations

import argparse

from _common import read_json, run_dir


def _observed_cell(observed: dict) -> str:
    """One-line summary of the observed branch state for the table."""
    if not observed:
        return "n/a"
    outcome = observed.get("outcome", "none")
    disabled = "yes" if observed.get("contact_disabled") else "no"
    replied = "yes" if observed.get("agent_replied") else "no"
    followup = "+task" if observed.get("followup_task") else ""
    return f"outcome={outcome}, disabled={disabled}, replied={replied}{followup}"


def critique_section(critique_doc: dict[str, object] | None) -> list[str]:
    """Build the Markdown lines for the advisory reply-handling critique.

    The critique judges the workflow's reply-handling wording using each
    scenario's reply and the agent's handling as evidence, and suggests edits.
    It is advisory and never changes the verdict.
    """
    if not critique_doc:
        return ["- critique: skipped"]

    score = critique_doc.get("overall_score")
    score_text = f"{score}/5" if score is not None else "n/a"
    summary = critique_doc.get("summary", "")
    patterns = critique_doc.get("patterns") or []
    weaknesses = critique_doc.get("weaknesses") or []
    edits = critique_doc.get("edits") or []

    lines = [
        f"- critique: ran, reply-handling wording score {score_text}",
        "",
        "## Workflow-wording critique (reply handling)",
        "",
        summary,
    ]
    if patterns:
        lines += ["", "### Patterns across the branches"]
        lines += [f"- {pattern}" for pattern in patterns]
    if weaknesses:
        lines += ["", "### Wording weaknesses"]
        lines += [f"- {weakness}" for weakness in weaknesses]
    if edits:
        lines += ["", "### Suggested workflow edits (highest impact first)"]
        lines += [f"{index}. {edit}" for index, edit in enumerate(edits, start=1)]
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    verify = read_json(directory / "verify.json")

    touch1_path = directory / "touch1.json"
    touch1 = read_json(touch1_path) if touch1_path.exists() else {"sends": []}
    replies_path = directory / "replies.json"
    replies = read_json(replies_path) if replies_path.exists() else {"replies": []}
    handled_path = directory / "handled.json"
    handled = read_json(handled_path) if handled_path.exists() else {"handled": []}

    critique_path = directory / "critiques.json"
    critique_doc = read_json(critique_path) if critique_path.exists() else None

    touch1_sent = sum(1 for s in touch1["sends"] if s.get("status") == "sent")
    replies_sent = sum(1 for r in replies["replies"] if r.get("reply_status") == "sent")
    handled_ok = sum(
        1 for h in handled["handled"] if h.get("handled_status") == "handled"
    )

    lines = [
        f"# Campaign reply-flow test report -- run {args.run_id}",
        "",
        f"Workflow under test: {manifest.get('workflow_name') or '(unknown)'} "
        f"(T1 path: {manifest.get('t1_mode') or 'unknown'}).",
        "",
        "The live outbound agent sent a cold Touch 1 to the prospect mailbox; the "
        "test replied with content crafted to drive each branch; the agent handled "
        "each reply. Every message went to the controlled inbound@lab5.ca mailbox, "
        "never a real prospect.",
        "",
        "| Scenario | Expected branch | Observed | Result |",
        "|---|---|---|---|",
    ]
    for result in verify["scenarios"]:
        observed = _observed_cell(result.get("observed", {}))
        verdict_cell = "PASS" if result.get("pass") else "FAIL"
        lines.append(
            f"| {result['scenario_key']} | {result.get('expected_branch', '')} | "
            f"{observed} | {verdict_cell} |"
        )

    verdict = verify["verdict"]
    lines += [
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- scenarios passed: {verify['passed']}/{verify['of']}",
        f"- Touch 1 sent: {touch1_sent}/{len(touch1['sends'])}",
        f"- replies injected: {replies_sent}/{len(replies['replies'])}",
        f"- replies handled: {handled_ok}/{len(handled['handled'])}",
    ]

    failures = [r for r in verify["scenarios"] if not r.get("pass")]
    if failures:
        lines += ["", "## Failing scenarios"]
        for result in failures:
            notes = "; ".join(result.get("notes", [])) or "(no detail)"
            lines.append(f"- {result['scenario_key']}: {notes}")

    lines += critique_section(critique_doc)

    report = "\n".join(lines) + "\n"
    (directory / "report.md").write_text(report)
    print(report)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
