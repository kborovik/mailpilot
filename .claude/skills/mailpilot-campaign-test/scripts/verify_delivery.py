"""Confirm each agent-sent message landed in the alias mailbox.

Syncs the alias mailbox (inbound@lab5.ca, which receives every inbound{1-9}
alias) from Gmail, then lists its inbound mail from the sender since the send
window and matches each sent message by the subject the agent wrote (captured in
``sends.json``). A live arrival proves the full send path -- account auth, Gmail
accept, alias routing, and render -- worked end to end. Subjects are
agent-generated rather than tagged, so matching is by exact subject; with at most
nine messages per run a collision is unlikely. Writes ``delivery.json``.

Usage:
    uv run python scripts/verify_delivery.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json

from _common import mp, read_json, run_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    sends = read_json(directory / "sends.json")
    alias_mailbox = sends["alias_mailbox"]
    sender_email = sends["sender_email"]
    window_start = sends["window_start"]

    # Expected: each sent message, keyed by its exact agent-written subject.
    subject_to_seq = {
        s["subject"]: s["sequence"]
        for s in sends["sends"]
        if s["status"] == "sent" and s["subject"]
    }
    expected = {s["sequence"] for s in sends["sends"] if s["status"] == "sent"}

    mp(["account", "sync", "--account-email", alias_mailbox], check=False)
    listing = mp(
        [
            "email",
            "list",
            "--account-email",
            alias_mailbox,
            "--from",
            sender_email,
            "--since",
            window_start,
            "--limit",
            "100",
        ],
        check=False,
    )

    arrived: set[int] = set()
    for row in listing.get("emails", []):
        seq = subject_to_seq.get(row.get("subject", ""))
        if seq is not None:
            arrived.add(seq)

    delivered = sorted(expected & arrived)
    missing = sorted(expected - arrived)
    result = {
        "alias_mailbox": alias_mailbox,
        "expected": sorted(expected),
        "delivered": delivered,
        "missing": missing,
    }
    write_json(directory / "delivery.json", result)

    print(json.dumps({"delivered": len(delivered), "missing": missing}, indent=2))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
