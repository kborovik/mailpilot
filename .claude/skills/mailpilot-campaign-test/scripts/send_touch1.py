"""Send the cold Touch 1 for every scenario through the live agent.

For each scenario it calls ``mailpilot enrollment run <enrollment_id>``
synchronously. The enrollment's workflow is outbound, so the agent runs an
initial reach-out: it reads the (mirrored) prospect contact and the real
grounding company, drafts a personalized Touch 1, and sends it -- through the
same send path the production agent uses, including the §V.42 body lint. The
recipient is the prospect contact's email (``inbound@lab5.ca``), so the message
can only reach the controlled mailbox.

After each run it reads back the outbound Touch 1 row for that scenario's
ephemeral workflow and captures the fields the reply step needs: the subject, the
Gmail thread id, and the RFC 2822 Message-ID -- the reply's ``In-Reply-To`` cites
this Message-ID, which is how routing matches the reply back to the ephemeral
outbound workflow. Writes ``touch1.json``.

Usage:
    uv run python scripts/send_touch1.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from _common import (
    PROSPECT_MAILBOX,
    SENDER_EMAIL,
    mp,
    read_json,
    run_dir,
    write_json,
)


def _run_enrollment(enrollment_id: str) -> dict:
    """Invoke the agent for one enrollment; return a normalized run record."""
    payload = mp(["enrollment", "run", enrollment_id], check=False)
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    return {
        "agent_run_status": payload.get("status", "failed"),
        "tool_calls": result.get("tool_calls", 0),
        "reasoning": result.get("reasoning", ""),
        "error": (
            result.get("reason")
            if payload.get("status") == "failed"
            else payload.get("error")
        ),
    }


def _read_touch1(workflow_id: str) -> dict | None:
    """Return the outbound Touch 1 row for a scenario's ephemeral workflow.

    At this point the ephemeral workflow has at most one outbound email (Touch 1
    has not been replied to yet), so the most recent outbound row is Touch 1.
    """
    listing = mp(
        [
            "email",
            "list",
            "--account-email",
            SENDER_EMAIL,
            "--workflow-id",
            workflow_id,
            "--direction",
            "outbound",
            "--limit",
            "10",
        ],
        check=False,
    )
    rows = listing.get("emails", [])
    if not rows:
        return None
    row = rows[0]
    view = mp(["email", "view", row["id"]], check=False)
    email = view.get("email", {}) if view.get("ok") else {}
    return {
        "outbound_email_id": row["id"],
        "subject": email.get("subject") or row.get("subject", ""),
        "gmail_thread_id": email.get("gmail_thread_id"),
        "rfc2822_message_id": email.get("rfc2822_message_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scaffold = read_json(directory / "scaffold.json")
    window_start = datetime.now().astimezone().isoformat()

    sends = []
    for entry in scaffold["entries"]:
        run = _run_enrollment(entry["enrollment_id"])
        touch1 = _read_touch1(entry["ephemeral_workflow_id"])
        status = "sent" if touch1 else "failed"
        sends.append(
            {
                "scenario_key": entry["scenario_key"],
                "ephemeral_workflow_id": entry["ephemeral_workflow_id"],
                "enrollment_id": entry["enrollment_id"],
                "status": status,
                "outbound_email_id": touch1["outbound_email_id"] if touch1 else None,
                "subject": touch1["subject"] if touch1 else None,
                "gmail_thread_id": touch1["gmail_thread_id"] if touch1 else None,
                "rfc2822_message_id": touch1["rfc2822_message_id"] if touch1 else None,
                "agent_run_status": run["agent_run_status"],
                "tool_calls": run["tool_calls"],
                "error": run["error"],
            }
        )

    result = {
        "window_start": window_start,
        "sender_email": SENDER_EMAIL,
        "prospect_mailbox": PROSPECT_MAILBOX,
        "sends": sends,
    }
    write_json(directory / "touch1.json", result)

    sent = sum(1 for s in sends if s["status"] == "sent")
    missing_msgid = [
        s["scenario_key"]
        for s in sends
        if s["status"] == "sent" and not s["rfc2822_message_id"]
    ]
    print(
        json.dumps(
            {
                "sent": sent,
                "failed": sum(1 for s in sends if s["status"] == "failed"),
                "of": len(sends),
                "missing_message_id": missing_msgid,
                "window_start": window_start,
            },
            indent=2,
        )
    )
    return 0 if sent and sent == len(sends) else 1


if __name__ == "__main__":
    raise SystemExit(main())
