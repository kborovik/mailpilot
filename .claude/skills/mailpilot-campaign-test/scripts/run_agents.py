"""Run the live workflow agent for each enrolled alias-contact.

This is the heart of the agentic test. For each enrollment it calls
``mailpilot enrollment run`` synchronously, which invokes the real outbound
workflow agent: the agent reads the (mirrored) contact and the real company,
drafts a personalized email, and sends it -- through the same send path the
production agent uses, including the §V.42 body lint. Because the alias-contact's
stored email IS the inbound alias, the agent can only reach the alias.

After all runs it reads back what the agent actually sent: the outbound email
rows for the ephemeral workflow give the agent-written subject and (via
``email view``) the body, captured for the delivery check and the critique.
Writes ``sends.json``.

Usage:
    uv run python scripts/run_agents.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

from _common import ALIAS_MAILBOX, SENDER_EMAIL, mp, read_json, run_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scaffold = read_json(directory / "scaffold.json")
    workflow_id = scaffold["ephemeral_workflow_id"]
    entries = scaffold["entries"]
    by_contact_id = {e["alias_contact_id"]: e for e in entries}

    window_start = datetime.now().astimezone().isoformat()

    # 1. Invoke the live agent per enrollment.
    agent_runs: dict[int, dict] = {}
    for entry in entries:
        payload = mp(["enrollment", "run", entry["enrollment_id"]], check=False)
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        agent_runs[entry["sequence"]] = {
            "status": payload.get("status", "failed"),
            "reasoning": result.get("reasoning", ""),
            "tool_calls": result.get("tool_calls", 0),
            "error": (
                result.get("reason")
                if payload.get("status") == "failed"
                else payload.get("error")
            ),
        }

    # 2. Read back what the agent actually sent (outbound rows for this run's
    #    ephemeral workflow), then fetch each body via ``email view``.
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
            "50",
        ],
        check=False,
    )
    email_by_seq: dict[int, dict] = {}
    for row in listing.get("emails", []):
        entry = by_contact_id.get(row.get("contact_id"))
        if entry is None:
            continue
        view = mp(["email", "view", row["id"]], check=False)
        body = view.get("email", {}).get("body_text", "") if view.get("ok") else ""
        email_by_seq[entry["sequence"]] = {
            "outbound_email_id": row["id"],
            "subject": row.get("subject", ""),
            "body": body,
            "gmail_message_id": view.get("email", {}).get("gmail_message_id"),
        }

    sends = []
    for entry in entries:
        seq = entry["sequence"]
        run = agent_runs.get(seq, {})
        email = email_by_seq.get(seq)
        if email is not None:
            status = "sent"
        elif run.get("status") == "skipped":
            status = "skipped"
        else:
            status = "failed"
        sends.append(
            {
                "sequence": seq,
                "alias": entry["alias"],
                "status": status,
                "subject": email["subject"] if email else None,
                "body": email["body"] if email else None,
                "outbound_email_id": email["outbound_email_id"] if email else None,
                "agent_run_status": run.get("status"),
                "tool_calls": run.get("tool_calls", 0),
                "error": run.get("error"),
            }
        )

    result = {
        "window_start": window_start,
        "alias_mailbox": ALIAS_MAILBOX,
        "sender_email": SENDER_EMAIL,
        "sends": sends,
    }
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
    return 0 if sent and not any(s["status"] == "failed" for s in sends) else 1


if __name__ == "__main__":
    raise SystemExit(main())
