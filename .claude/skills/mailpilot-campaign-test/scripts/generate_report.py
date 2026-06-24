"""Fold every phase into one Markdown report and an overall verdict.

Joins the send (agent-run), delivery, and critique artifacts into a per-contact
table plus a PASS/FAIL verdict. PASS requires zero send failures and -- when the
delivery check ran -- zero missing deliveries. A send failure here means the live
agent did not produce a deliverable email (an agent error, or a body that never
cleared the §V.42 lint inside the send path). The workflow-wording critique is
advisory: it adds a wording-score line and a critique section that suggests edits
to the workflow, but never changes the verdict. Writes ``report.md`` and prints
it.

Usage:
    uv run python scripts/generate_report.py --run-id <id>
"""

from __future__ import annotations

import argparse

from _common import read_json, run_dir


def critique_section(
    critique_ran: bool,
    critique_doc: dict[str, object] | None,
) -> list[str]:
    """Build the Markdown lines for the advisory workflow-wording critique.

    The critique judges the workflow's ``objective`` and ``instructions`` -- the
    wording that drove the agent -- using the sent emails as evidence, and
    suggests edits to that wording. It is advisory and never changes the verdict.

    Args:
        critique_ran: Whether the critique phase produced an artifact.
        critique_doc: The workflow-wording critique document, or None when
            skipped.

    Returns:
        The Markdown lines for the critique section.
    """
    if not critique_ran or not critique_doc:
        return ["- critique: skipped"]

    score = critique_doc.get("overall_score")
    score_text = f"{score}/5" if score is not None else "n/a"
    summary = critique_doc.get("summary", "")
    patterns = critique_doc.get("patterns") or []
    weaknesses = critique_doc.get("weaknesses") or []
    edits = critique_doc.get("edits") or []

    lines = [
        f"- critique: ran, workflow-wording score {score_text}",
        "",
        "## Workflow-wording critique",
        "",
        summary,
    ]
    if patterns:
        lines += ["", "### Patterns across the emails"]
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
    contact_by_seq = {c["sequence"]: c for c in manifest["contacts"]}
    sends = read_json(directory / "sends.json")["sends"]

    delivery_path = directory / "delivery.json"
    delivery = read_json(delivery_path) if delivery_path.exists() else None
    delivered = set(delivery["delivered"]) if delivery else set()
    delivery_ran = delivery is not None

    critique_path = directory / "critiques.json"
    critique_doc = read_json(critique_path) if critique_path.exists() else None
    critique_ran = critique_doc is not None

    lines = [
        f"# Campaign test report -- run {args.run_id}",
        "",
        f"Workflow under test: {manifest.get('workflow_name') or '(unknown)'}.",
        "",
        "The live outbound agent drafted and sent each email. Real contacts "
        "supplied personalization data (mirrored onto an alias-contact); every "
        "message was sent to an inbound alias, never to the real address.",
        "",
        "| # | Contact | Sent to | Agent run | Send | Delivered |",
        "|---|---|---|---|---|---|",
    ]
    send_failures = 0
    for send in sorted(sends, key=lambda s: s["sequence"]):
        seq = send["sequence"]
        contact = contact_by_seq.get(seq, {})
        if send.get("status") == "failed":
            send_failures += 1
        delivered_cell = "n/a"
        if delivery_ran:
            delivered_cell = "yes" if seq in delivered else "no"
        lines.append(
            f"| {seq} | {contact.get('email', '?')} | {send.get('alias', '?')} | "
            f"{send.get('agent_run_status', 'n/a')} | {send.get('status', 'n/a')} | "
            f"{delivered_cell} |"
        )

    missing = delivery["missing"] if delivery_ran else []
    passed = send_failures == 0 and (not delivery_ran or not missing)
    verdict = "PASS" if passed else "FAIL"

    lines += [
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- emails sent: {sum(1 for s in sends if s['status'] == 'sent')}/{len(sends)}",
        f"- send failures: {send_failures}",
        f"- delivery check: {'ran' if delivery_ran else 'skipped'}"
        + (f", missing {missing}" if delivery_ran and missing else ""),
    ]

    # Surface any send failure reasons so a FAIL is actionable.
    failures = [s for s in sends if s.get("status") == "failed" and s.get("error")]
    if failures:
        lines.append("")
        lines.append("## Send failures")
        for send in sorted(failures, key=lambda s: s["sequence"]):
            lines.append(f"- #{send['sequence']} ({send['alias']}): {send['error']}")

    lines += critique_section(critique_ran, critique_doc)

    report = "\n".join(lines) + "\n"
    (directory / "report.md").write_text(report)
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
