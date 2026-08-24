"""Drive the agent's reply handling for every scenario, scoped -- no run loop.

This is the second agent turn of the multi-step flow. It reproduces exactly what
the production sync loop does when a reply arrives, but scoped to this run so no
unscoped ``mailpilot run`` is started (which would sync every account and could
auto-reply to genuine mail):

  1. Sync the sender mailbox (``outbound@lab5.ca``) so each reply is ingested and
     routed. Routing matches a reply to its scenario's ephemeral outbound
     workflow by RFC 2822 Message-ID (``In-Reply-To`` cites the Touch 1
     Message-ID); classification never runs because the ephemeral workflows are
     outbound. Polls until every reply has routed or the timeout elapses.
     Mechanical inject already sent ``Automatic reply`` subject, so
     ``_maybe_ooo_pause`` fires here (§V.188 / §B.150).
  2. ``create_tasks_for_routed_emails`` bridges each routed reply into a "handle
     inbound email" task (the same call the loop makes). Mechanical OOO is a
     completed processed marker instead, so later sync does not re-enqueue.
  3. For each scenario in turn, ``execute_task`` invokes the live agent on its
     reply -- the agent reads the inbound message and takes one branch (reply,
     conclude, disable, or noop). Mechanical OOO records the processed marker
     and does not invoke. Leftover handle-inbound tasks complete without a
     second resume.

Because all scenarios share one prospect contact, an opt-out / wrong-person
branch disables that contact globally, which would make a later scenario's task
skip (a disabled contact is not actioned). So scenarios are handled one at a
time: the contact is re-enabled before each, its notes from a prior branch's
``conclude_enrollment`` are cleared (pre-fed grounding must not carry another
branch's outcome), its disabled state is snapshotted right after (that snapshot
is the branch signal the verify step reads), then it is re-enabled again for
the next scenario. Writes ``handled.json``.

Usage:
    uv run python scripts/handle_replies.py --run-id <id> [--timeout N] [--poll N]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from _common import (
    PROSPECT_EMAIL,
    SENDER_EMAIL,
    clear_contact_notes,
    mp,
    read_json,
    repo_root,
    run_dir,
    write_json,
)

DEFAULT_TIMEOUT = 240


def _contact_disabled_reason() -> str | None:
    """Return the prospect contact's disabled_reason, or None when active."""
    view = mp(["contact", "view", PROSPECT_EMAIL], check=False)
    return view.get("contact", {}).get("disabled_reason") if view.get("ok") else None


def _ensure_enabled() -> None:
    """Re-enable the prospect contact if a prior branch disabled it."""
    if _contact_disabled_reason() is not None:
        mp(["contact", "enable", PROSPECT_EMAIL], check=False)


def _read_task_result(task_id: str) -> dict:
    """Return {task_status, tool_calls, reasoning} read back from the task row."""
    view = mp(["task", "view", task_id], check=False)
    task = view.get("task", {}) if view.get("ok") else {}
    result = task.get("result") or {}
    return {
        "task_status": task.get("status"),
        "tool_calls": result.get("tool_calls"),
        "reasoning": result.get("reasoning"),
    }


def _routed_reply_id(workflow_id: str) -> str | None:
    """Return the routed inbound reply id on an ephemeral workflow, or None."""
    listing = mp(
        [
            "email",
            "list",
            "--account-email",
            SENDER_EMAIL,
            "--workflow-id",
            workflow_id,
            "--direction",
            "inbound",
            "--limit",
            "10",
        ],
        check=False,
    )
    rows = listing.get("emails", [])
    return rows[0]["id"] if rows else None


def _wait_for_routing(
    handle_keys: list[str],
    entries_by_key: dict[str, dict],
    expected_wf: set[str],
    timeout: int,
    poll: int,
) -> dict[str, str]:
    """Sync + route until every reply attaches to its workflow, or timeout.

    Returns ``{ephemeral_workflow_id: routed_inbound_reply_id}``.
    """
    routed: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    while set(routed) != expected_wf and time.monotonic() < deadline:
        mp(["account", "sync", "--account-email", SENDER_EMAIL], check=False)
        for key in handle_keys:
            workflow_id = entries_by_key[key]["ephemeral_workflow_id"]
            if workflow_id in routed:
                continue
            reply_id = _routed_reply_id(workflow_id)
            if reply_id:
                routed[workflow_id] = reply_id
        if set(routed) != expected_wf:
            print(
                f"[handle] {len(routed)}/{len(expected_wf)} replies routed, waiting...",
                file=sys.stderr,
            )
            time.sleep(poll)
    return routed


def _completed_inbound_task(workflow_id: str, email_id: str | None) -> dict | None:
    """Return the completed task for a routed inbound, or None.

    Mechanical OOO inserts a completed processed marker instead of
    ``handle inbound email`` (§V.188). Handle records that marker so
    verify still sees ``task_status=completed``.
    """
    if not email_id:
        return None
    listing = mp(
        [
            "task",
            "list",
            "--workflow-id",
            workflow_id,
            "--status",
            "completed",
            "--limit",
            "50",
        ],
        check=False,
    )
    for task in listing.get("tasks", []):
        if task.get("email_id") == email_id:
            return task
    return None


def _handle_scenario(
    entry: dict,
    routed: dict[str, str],
    tasks_by_workflow: dict[str, list],
    settings: object,
    database_url: str,
) -> dict:
    """Invoke the agent on one scenario's reply and snapshot the result.

    Re-enables the prospect contact before running (a prior branch may have
    disabled it), clears notes left by a prior branch's conclude_enrollment
    (shared prospect contact; notes would poison pre-fed grounding like §B.118
    but mid-run), then snapshots disabled state right after -- that snapshot
    is the branch signal the verify step reads.
    """
    import psycopg
    from psycopg.rows import dict_row

    from mailpilot.run import execute_task

    workflow_id = entry["ephemeral_workflow_id"]
    reply_email_id = routed.get(workflow_id)
    record: dict[str, object] = {
        "scenario_key": entry["scenario_key"],
        "ephemeral_workflow_id": workflow_id,
        "enrollment_id": entry["enrollment_id"],
        "routed": workflow_id in routed,
    }

    tasks = tasks_by_workflow.get(workflow_id, [])
    task = next((t for t in tasks if t.email_id == reply_email_id), None)
    if task is None and tasks:
        task = tasks[0]
    _ensure_enabled()
    # Shared prospect: prior branch may have written a conclude_enrollment note.
    clear_contact_notes(PROSPECT_EMAIL)
    if task is None:
        marker = _completed_inbound_task(workflow_id, reply_email_id)
        if marker is not None:
            record["task_id"] = marker["id"]
            record["handled_status"] = "handled"
            record["task_status"] = marker.get("status")
            reason = _contact_disabled_reason()
            record["contact_disabled_after"] = reason is not None
            record["disabled_reason_after"] = reason
            if reason is not None:
                mp(["contact", "enable", PROSPECT_EMAIL], check=False)
            return record
        record["handled_status"] = "no_task" if record["routed"] else "unrouted"
        return record

    record["task_id"] = task.id
    worker_conn = psycopg.connect(database_url, row_factory=dict_row)
    try:
        execute_task(worker_conn, settings, task)
        worker_conn.commit()
        record["handled_status"] = "handled"
    except Exception as exc:
        worker_conn.rollback()
        record["handled_status"] = "error"
        record["error"] = str(exc)
    finally:
        worker_conn.close()

    record.update(_read_task_result(task.id))
    reason = _contact_disabled_reason()
    record["contact_disabled_after"] = reason is not None
    record["disabled_reason_after"] = reason
    if reason is not None:
        mp(["contact", "enable", PROSPECT_EMAIL], check=False)
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--poll", type=int, default=8)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scaffold = read_json(directory / "scaffold.json")
    replies = read_json(directory / "replies.json")
    entries_by_key = {e["scenario_key"]: e for e in scaffold["entries"]}

    # Only scenarios whose reply was actually sent can be handled.
    handle_keys = [
        r["scenario_key"] for r in replies["replies"] if r["reply_status"] == "sent"
    ]
    expected_wf = {entries_by_key[k]["ephemeral_workflow_id"] for k in handle_keys}

    handle_window_start = datetime.now().astimezone().isoformat()

    sys.path.insert(0, str(repo_root() / "src"))
    import psycopg
    from psycopg.rows import dict_row

    from mailpilot.cli import configure_logging
    from mailpilot.database import create_tasks_for_routed_emails
    from mailpilot.settings import get_settings

    # This step runs the agent in-process via ``execute_task``; mirror the CLI so
    # pydantic-ai's chat and tool spans reach Logfire (issue #163). Console output
    # goes to stderr at warn level, so the stdout JSON envelope stays clean (§V.3).
    configure_logging()

    settings = get_settings()
    database_url = str(settings.database_url)

    # 1. Sync + route until every reply is attached to its ephemeral workflow.
    routed = _wait_for_routing(
        handle_keys, entries_by_key, expected_wf, args.timeout, args.poll
    )

    # 2. Bridge each routed reply into a task (returns the created Task rows).
    bridge_conn = psycopg.connect(database_url, row_factory=dict_row)
    try:
        created_tasks = create_tasks_for_routed_emails(bridge_conn)
        bridge_conn.commit()
    finally:
        bridge_conn.close()
    tasks_by_workflow: dict[str, list] = {}
    for task in created_tasks:
        tasks_by_workflow.setdefault(task.workflow_id, []).append(task)

    # 3. Handle one scenario at a time, re-enabling the contact between each.
    handled = [
        _handle_scenario(
            entries_by_key[key],
            routed,
            tasks_by_workflow,
            settings,
            database_url,
        )
        for key in handle_keys
    ]

    write_json(
        directory / "handled.json",
        {"handle_window_start": handle_window_start, "handled": handled},
    )

    ok = sum(1 for h in handled if h.get("handled_status") == "handled")
    print(
        json.dumps(
            {
                "handled": ok,
                "of": len(handled),
                "unrouted": [
                    h["scenario_key"]
                    for h in handled
                    if h.get("handled_status") in ("unrouted", "no_task")
                ],
                "handle_window_start": handle_window_start,
            },
            indent=2,
        )
    )
    return 0 if ok and ok == len(handled) else 1


if __name__ == "__main__":
    raise SystemExit(main())
