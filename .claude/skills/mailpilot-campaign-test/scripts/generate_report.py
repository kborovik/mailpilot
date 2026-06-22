"""Fold every phase into one Markdown report and an overall verdict.

Joins the personalization, lint, send, and (when present) delivery and critique
artifacts into a per-contact table plus a PASS/FAIL verdict. PASS requires zero
lint failures, zero send failures, and -- when the delivery check ran -- zero
missing deliveries. Personalization gaps are warnings, not failures. The
marketing critique is advisory: it adds a score column and a critique section but
never changes the verdict. Writes ``report.md`` and prints it.

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
    rendered = read_json(directory / "personalized.json")["rendered"]
    sends = read_json(directory / "sends.json")["sends"]
    send_by_seq = {s["sequence"]: s for s in sends}

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
        "Real contacts supplied personalization data only. Each message was sent "
        "to the contact's assigned inbound alias, never to the contact's own "
        "address.",
        "",
        "| # | Contact | Alias | Gaps | Lint | Send | Delivered | Critique |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lint_failures = send_failures = 0
    for entry in rendered:
        seq = entry["sequence"]
        send = send_by_seq.get(seq, {})
        if entry["lint"] == "fail":
            lint_failures += 1
        if send.get("status") == "failed":
            send_failures += 1
        delivered_cell = "n/a"
        if delivery_ran:
            delivered_cell = "yes" if seq in delivered else "no"
        critique = critique_by_seq.get(seq)
        critique_cell = f"{critique['overall_score']}/5" if critique else "n/a"
        gaps = "; ".join(entry["gaps"]) if entry["gaps"] else "none"
        lines.append(
            f"| {seq} | {entry['contact_email']} | {entry['alias']} | {gaps} | "
            f"{entry['lint']} | {send.get('status', 'n/a')} | {delivered_cell} | "
            f"{critique_cell} |"
        )

    missing = delivery["missing"] if delivery_ran else []
    passed = (
        lint_failures == 0 and send_failures == 0 and (not delivery_ran or not missing)
    )
    verdict = "PASS" if passed else "FAIL"

    lines += [
        "",
        f"**Verdict: {verdict}**",
        "",
        f"- lint failures: {lint_failures}",
        f"- send failures: {send_failures}",
        f"- delivery check: {'ran' if delivery_ran else 'skipped'}"
        + (f", missing {missing}" if delivery_ran and missing else ""),
    ]

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
            send = send_by_seq.get(critique["sequence"], {})
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
