"""Fold every phase into one Markdown report and an overall verdict.

Joins the send (agent-run), delivery, and critique artifacts into a per-contact
table plus a PASS/FAIL verdict. PASS requires zero send failures and -- when the
delivery check ran -- zero missing deliveries. A send failure here means the live
agent did not produce a deliverable email (an agent error, or a body that never
cleared the §V.42 lint inside the send path). The marketing critique is advisory:
it adds a score column and a critique section but never changes the verdict.
Writes ``report.md`` and prints it.

Usage:
    uv run python scripts/generate_report.py --run-id <id>
"""

from __future__ import annotations

import argparse

from _common import read_json, run_dir


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
    critiques = critique_doc.get("critiques", []) if critique_doc else []
    critique_by_seq = {c["sequence"]: c for c in critiques}
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
        "| # | Contact | Sent to | Agent run | Send | Delivered | Critique |",
        "|---|---|---|---|---|---|---|",
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
        critique = critique_by_seq.get(seq)
        critique_cell = f"{critique['overall_score']}/5" if critique else "n/a"
        lines.append(
            f"| {seq} | {contact.get('email', '?')} | {send.get('alias', '?')} | "
            f"{send.get('agent_run_status', 'n/a')} | {send.get('status', 'n/a')} | "
            f"{delivered_cell} | {critique_cell} |"
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

    if critique_ran and critiques:
        scores = [c["overall_score"] for c in critiques]
        average = sum(scores) / len(scores)
        lines += [
            f"- critique: ran, average score {average:.1f}/5 over {len(scores)} emails",
            "",
            "## Marketing critique",
            "",
            critique_doc.get("summary", ""),
        ]
        for critique in sorted(critiques, key=lambda c: c["sequence"]):
            send = next(
                (s for s in sends if s["sequence"] == critique["sequence"]), {}
            )
            suggestions = critique.get("suggestions") or []
            top = suggestions[0] if suggestions else "(no suggestion)"
            lines += [
                "",
                f"### Email {critique['sequence']} -- {send.get('alias', '?')} "
                f"(score {critique['overall_score']}/5)",
                f"- top fix: {top}",
            ]
    elif critique_ran:
        lines.append("- critique: ran, no emails critiqued")
    else:
        lines.append("- critique: skipped")

    report = "\n".join(lines) + "\n"
    (directory / "report.md").write_text(report)
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
