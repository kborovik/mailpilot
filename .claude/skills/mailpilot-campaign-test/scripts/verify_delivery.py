"""Confirm each sent test message landed in the alias mailbox.

Syncs the alias mailbox (inbound@lab5.ca, which receives every inbound{1-9}
alias) from Gmail, then lists its inbound mail since the send window and matches
each sent message by its ``[CAMPAIGN-TEST <run_id> <seq>]`` subject tag. A live
arrival proves the full send path -- account auth, Gmail accept, alias routing,
and render -- worked end to end. Writes ``delivery.json``.

Usage:
    uv run python scripts/verify_delivery.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json

from _common import (
    SENDER_EMAIL,
    SUBJECT_TAG_RE,
    mp,
    read_json,
    run_dir,
    write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    sends = read_json(directory / "sends.json")
    alias_mailbox = sends["alias_mailbox"]
    window_start = sends["window_start"]
    expected = {s["sequence"] for s in sends["sends"] if s["status"] == "sent"}

    mp(["account", "sync", "--account-email", alias_mailbox], check=False)
    listing = mp(
        [
            "email",
            "list",
            "--account-email",
            alias_mailbox,
            "--from",
            SENDER_EMAIL,
            "--since",
            window_start,
            "--limit",
            "100",
        ],
        check=False,
    )

    arrived: set[int] = set()
    for row in listing.get("emails", []):
        match = SUBJECT_TAG_RE.search(row.get("subject", ""))
        if match is not None and match.group("run_id") == args.run_id:
            arrived.add(int(match.group("seq")))

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
