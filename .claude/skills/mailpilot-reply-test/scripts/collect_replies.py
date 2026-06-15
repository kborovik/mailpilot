"""Poll for the agent's threaded replies and capture them for grading.

Lists outbound emails on the inbound account since window_start, matches the
MP-TEST subject tag to map reply -> case, fetches the full body via
``email view``, and resolves the inbound trigger email id (same Gmail thread) so
Logfire token/latency rows -- keyed on ``agent.invoke.email_id`` -- can be joined
per case. Stops when every expected case has replied or the timeout elapses; a
missing reply is recorded as ``no_reply`` (a real failure mode).

Usage:
    uv run python scripts/collect_replies.py --run-id <id> --run A|B [--timeout N] [--poll N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from _common import mp, parse_subject, read_json, run_dir, write_json

DEFAULT_TIMEOUT = {"A": 300, "B": 480}


def _resolve_trigger_id(
    inbound_id: str, thread_id: str | None, run_id: str, case_id: str
) -> str | None:
    if not thread_id:
        return None
    data = mp(
        [
            "email",
            "list",
            "--account-id",
            inbound_id,
            "--thread-id",
            thread_id,
            "--direction",
            "inbound",
            "--limit",
            "5",
        ],
        check=False,
    )
    for email in data.get("emails", []):
        parsed = parse_subject(email.get("subject", ""))
        if parsed == (run_id, case_id):
            return email.get("id")
    # Fall back to the earliest inbound message in the thread.
    emails = data.get("emails", [])
    return emails[-1].get("id") if emails else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run", required=True, choices=["A", "B"])
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--poll", type=int, default=8)
    args = parser.parse_args()

    timeout = args.timeout or DEFAULT_TIMEOUT[args.run]
    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    sends = read_json(directory / f"sends_{args.run}.json")
    run_id = manifest["run_id"]
    inbound_id = manifest["inbound_account_id"]
    window_start = sends["window_start"]

    expected = {c["case_id"] for c in manifest["cases"] if c["run"] == args.run}
    pending = set(expected)
    replies: dict[str, dict] = {}

    started = time.monotonic()
    deadline = started + timeout
    while pending and time.monotonic() < deadline:
        listing = mp(
            [
                "email",
                "list",
                "--account-id",
                inbound_id,
                "--direction",
                "outbound",
                "--since",
                window_start,
                "--status",
                "sent",
                "--limit",
                "50",
            ],
            check=False,
        )
        for email in listing.get("emails", []):
            parsed = parse_subject(email.get("subject", ""))
            if parsed is None:
                continue
            r_run, case_id = parsed
            if r_run != run_id or case_id not in pending:
                continue
            view = mp(["email", "view", email["id"]], check=False)
            body = str(view.get("email", {}).get("body_text", ""))
            thread_id = email.get("gmail_thread_id")
            replies[case_id] = {
                "status": "replied",
                "reply_email_id": email["id"],
                "trigger_email_id": _resolve_trigger_id(
                    inbound_id, thread_id, run_id, case_id
                ),
                "gmail_thread_id": thread_id,
                "subject": email.get("subject"),
                "sent_at": email.get("sent_at"),
                "body": body,
            }
            pending.discard(case_id)
        if pending:
            print(
                f"[collect {args.run}] {len(expected) - len(pending)}/{len(expected)} replied, waiting...",
                file=sys.stderr,
            )
            time.sleep(args.poll)

    for case_id in pending:
        replies[case_id] = {"status": "no_reply"}

    elapsed = round(time.monotonic() - started, 1)
    write_json(
        directory / f"replies_{args.run}.json",
        {"run": args.run, "elapsed_seconds": elapsed, "replies": replies},
    )
    print(
        json.dumps(
            {
                "run": args.run,
                "replied": len(expected) - len(pending),
                "no_reply": len(pending),
                "elapsed_seconds": elapsed,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
