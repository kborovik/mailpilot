"""Reply to each scenario's Touch 1 from the prospect mailbox.

This is what makes the test multi-step: it plays the prospect. It syncs the
prospect mailbox (``inbound@lab5.ca``), finds each scenario's received Touch 1 --
matched by RFC 2822 Message-ID, which Gmail preserves from the sent copy, so the
match is exact and never relies on collision-prone subjects -- and replies to it
with the scenario's crafted body via ``mailpilot email reply``. The reply is sent
from ``inbound@lab5.ca`` back to ``outbound@lab5.ca`` in-thread, so its
``In-Reply-To`` cites the Touch 1 Message-ID and routing can match it back to the
scenario's ephemeral outbound workflow.

Polls the sync until every sent Touch 1 has arrived or the timeout elapses (Gmail
delivery is not instant). Writes ``replies.json``.

Usage:
    uv run python scripts/inject_replies.py --run-id <id> [--timeout N] [--poll N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from _common import (
    PROSPECT_MAILBOX,
    SENDER_EMAIL,
    load_scenarios,
    mp,
    read_json,
    run_dir,
    write_json,
)

DEFAULT_TIMEOUT = 300


def _received_index(window_start: str) -> dict[str, str]:
    """Map received Touch 1 RFC Message-ID -> received email id (and subject).

    Lists inbound mail at the prospect mailbox from the sender since the send
    window, then resolves each row's RFC Message-ID via ``email view`` (the list
    projection omits it). Returns ``{rfc2822_message_id: received_email_id}`` and,
    under the ``"__subjects__"`` key, ``{subject: received_email_id}`` for the
    fallback match when a Touch 1 row never captured its Message-ID.
    """
    listing = mp(
        [
            "email",
            "list",
            "--account-email",
            PROSPECT_MAILBOX,
            "--from",
            SENDER_EMAIL,
            "--direction",
            "inbound",
            "--since",
            window_start,
            "--limit",
            "100",
        ],
        check=False,
    )
    by_msgid: dict[str, str] = {}
    by_subject: dict[str, str] = {}
    for row in listing.get("emails", []):
        view = mp(["email", "view", row["id"]], check=False)
        email = view.get("email", {}) if view.get("ok") else {}
        msgid = email.get("rfc2822_message_id")
        if msgid:
            by_msgid[msgid] = row["id"]
        subject = email.get("subject") or row.get("subject")
        if subject:
            by_subject.setdefault(subject, row["id"])
    by_msgid["__subjects__"] = json.dumps(by_subject)
    return by_msgid


def _match(send: dict, index: dict[str, str]) -> str | None:
    """Return the received Touch 1 email id for a scenario send, or None.

    Message-ID is authoritative (§V.122). Subject fallback only when Message-ID
    was never captured -- and never when multiple scenarios share a subject
    (Gmail merges + setdefault would attach the wrong Touch 1).
    """
    msgid = send.get("rfc2822_message_id")
    if msgid and msgid in index:
        return index[msgid]
    if msgid:
        # Captured Message-ID not in the index yet -- wait for sync; do not
        # fall back to collision-prone subjects while the real row is pending.
        return None
    subjects = json.loads(index.get("__subjects__", "{}"))
    return subjects.get(send.get("subject"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--poll", type=int, default=8)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    touch1 = read_json(directory / "touch1.json")
    sends = [s for s in touch1["sends"] if s["status"] == "sent"]
    bodies = {s["key"]: s["reply_body"] for s in load_scenarios()}

    expected = {s["scenario_key"] for s in sends}
    found: dict[str, str] = {}

    started = time.monotonic()
    deadline = started + args.timeout
    while set(found) != expected and time.monotonic() < deadline:
        mp(["account", "sync", "--account-email", PROSPECT_MAILBOX], check=False)
        index = _received_index(touch1["window_start"])
        for send in sends:
            key = send["scenario_key"]
            if key in found:
                continue
            received_id = _match(send, index)
            if received_id:
                found[key] = received_id
        if set(found) != expected:
            print(
                f"[inject] {len(found)}/{len(expected)} Touch 1s arrived, waiting...",
                file=sys.stderr,
            )
            time.sleep(args.poll)

    reply_window_start = datetime.now().astimezone().isoformat()
    replies = []
    for send in sends:
        key = send["scenario_key"]
        received_id = found.get(key)
        if received_id is None:
            replies.append({"scenario_key": key, "reply_status": "touch1_not_received"})
            continue
        payload = mp(
            [
                "email",
                "reply",
                "--account-email",
                PROSPECT_MAILBOX,
                "--email-id",
                received_id,
                "--body",
                bodies[key],
            ],
            check=False,
        )
        email = payload.get("email", {}) if isinstance(payload, dict) else {}
        replies.append(
            {
                "scenario_key": key,
                "reply_status": "sent" if payload.get("ok") else "failed",
                "received_touch1_email_id": received_id,
                "reply_email_id": email.get("id"),
                "error": payload.get("error"),
            }
        )

    result = {
        "reply_window_start": reply_window_start,
        "prospect_mailbox": PROSPECT_MAILBOX,
        "sender_email": SENDER_EMAIL,
        "replies": replies,
    }
    write_json(directory / "replies.json", result)

    sent = sum(1 for r in replies if r["reply_status"] == "sent")
    print(
        json.dumps(
            {
                "replies_sent": sent,
                "of": len(replies),
                "not_received": [
                    r["scenario_key"]
                    for r in replies
                    if r["reply_status"] == "touch1_not_received"
                ],
                "reply_window_start": reply_window_start,
            },
            indent=2,
        )
    )
    return 0 if sent and sent == len(replies) else 1


if __name__ == "__main__":
    raise SystemExit(main())
