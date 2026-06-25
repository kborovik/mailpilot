"""Confirm each agent-sent message landed in the alias mailbox.

Syncs the alias mailbox (inbound@lab5.ca, which receives every inbound{1-9}
alias) from Gmail, then lists its inbound mail from the sender since the send
window and matches each sent message by its unique recipient alias, never the
agent-written subject. Each sent sequence owns one alias (``sends[].alias``);
the alias rides the received To header, preserved through catch-all routing into
the alias mailbox, so the single bulk ``email list`` recipients projection
(§V.7) reveals which sequence each arrival belongs to. Subjects are
agent-generated and collision-prone, so two sends that share a subject would
both count delivered under the alias key (§V.122, closes §B.108). A live arrival
proves the full send path -- account auth, Gmail accept, alias routing, and
render -- worked end to end. Writes ``delivery.json``.

Usage:
    uv run python scripts/verify_delivery.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
from typing import Any


def compute_delivery(
    sends: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Match each arrival to its sequence by recipient alias, never subject.

    Each sent sequence owns a unique inbound alias (``sends[].alias``); the alias
    rides the received To header, preserved through catch-all routing into the
    alias mailbox (§V.122). Two sends that happen to share an agent-written
    subject both count delivered, because the alias -- not the subject -- is the
    identity key (closes §B.108).

    Args:
        sends: Parsed ``sends.json`` carrying ``alias_mailbox`` and a ``sends``
            list of ``{sequence, alias, status, subject}`` entries.
        rows: ``email list`` rows, each carrying a ``recipients`` map
            (``{"to": [...], "cc": [...], "bcc": [...]}`` per §V.7).

    Returns:
        Result dict ``{alias_mailbox, expected, delivered, missing}`` with the
        sequence lists sorted ascending.
    """
    alias_to_sequence = {
        send["alias"].lower(): send["sequence"]
        for send in sends["sends"]
        if send["status"] == "sent" and send.get("alias")
    }
    expected = {send["sequence"] for send in sends["sends"] if send["status"] == "sent"}

    arrived: set[int] = set()
    for row in rows:
        addresses = {
            address.lower()
            for group in row.get("recipients", {}).values()
            for address in group
        }
        for alias, sequence in alias_to_sequence.items():
            if alias in addresses:
                arrived.add(sequence)

    return {
        "alias_mailbox": sends["alias_mailbox"],
        "expected": sorted(expected),
        "delivered": sorted(expected & arrived),
        "missing": sorted(expected - arrived),
    }


def main() -> int:
    from _common import mp, read_json, run_dir, write_json

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    sends = read_json(directory / "sends.json")
    alias_mailbox = sends["alias_mailbox"]
    sender_email = sends["sender_email"]
    window_start = sends["window_start"]

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

    result = compute_delivery(sends, listing.get("emails", []))
    write_json(directory / "delivery.json", result)

    print(
        json.dumps(
            {"delivered": len(result["delivered"]), "missing": result["missing"]},
            indent=2,
        )
    )
    return 0 if not result["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
