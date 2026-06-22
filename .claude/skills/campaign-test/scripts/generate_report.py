"""Fold every phase into one Markdown report and an overall verdict.

Joins the personalization, lint, send, and (when present) delivery artifacts
into a per-contact table plus a PASS/FAIL verdict. PASS requires zero lint
failures, zero send failures, and -- when the delivery check ran -- zero missing
deliveries. Personalization gaps are surfaced as warnings, not failures. Writes
``report.md`` and prints it.

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

    lines = [
        f"# Campaign test report -- run {args.run_id}",
        "",
        "Discovered contacts supplied personalization data only. Every message "
        "was sent to the sink mailbox, never to a contact's own address.",
        "",
        "| # | Contact | Gaps | Lint | Send | Delivered |",
        "|---|---|---|---|---|---|",
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
        gaps = "; ".join(entry["gaps"]) if entry["gaps"] else "none"
        lines.append(
            f"| {seq} | {entry['contact_email']} | {gaps} | {entry['lint']} | "
            f"{send.get('status', 'n/a')} | {delivered_cell} |"
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
    report = "\n".join(lines) + "\n"
    (directory / "report.md").write_text(report)
    print(report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
