"""Live-send each personalized campaign message to the sink mailbox.

Sends FROM the sender account TO the sink mailbox -- never to the discovered
contact's own address. Lint failures (§V.42) are skipped by default so a
malformed body never goes out; pass ``--include-lint-failures`` to send them
anyway. Records ``window_start`` (captured before the first send) as the
``--since`` lower bound the delivery check uses. Writes ``sends.json``.

Usage:
    uv run python scripts/send_campaign.py --run-id <id> [--include-lint-failures]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from _common import mp, read_json, run_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--include-lint-failures", action="store_true")
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    rendered = read_json(directory / "personalized.json")["rendered"]
    sender_id = manifest["sender_account_id"]
    sink_email = manifest["sink_email"]

    window_start = datetime.now().astimezone().isoformat()
    sends = []
    for entry in rendered:
        if entry["lint"] == "fail" and not args.include_lint_failures:
            sends.append(
                {
                    "sequence": entry["sequence"],
                    "status": "skipped",
                    "reason": "lint_failure",
                    "subject": entry["subject"],
                }
            )
            continue
        payload = mp(
            [
                "email",
                "send",
                "--account-email",
                sender_id,
                "--to",
                sink_email,
                "--subject",
                entry["subject"],
                "--body",
                entry["body"],
            ],
            check=False,
        )
        email = payload.get("email", {})
        sends.append(
            {
                "sequence": entry["sequence"],
                "status": "sent" if payload.get("ok") else "failed",
                "subject": entry["subject"],
                "outbound_email_id": email.get("id"),
                "error": payload.get("error"),
            }
        )

    result = {"window_start": window_start, "sink_email": sink_email, "sends": sends}
    write_json(directory / "sends.json", result)

    sent = sum(1 for s in sends if s["status"] == "sent")
    print(
        json.dumps(
            {
                "sent": sent,
                "failed": sum(1 for s in sends if s["status"] == "failed"),
                "skipped": sum(1 for s in sends if s["status"] == "skipped"),
                "of": len(sends),
                "window_start": window_start,
            },
            indent=2,
        )
    )
    failed = any(s["status"] == "failed" for s in sends)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
