"""Verify each scenario drove the agent into the right reply branch.

For every scenario it reads the observable state the agent's handling left
behind and compares it to the scenario's expectations in the catalog:

  - **outcome** -- the enrollment's terminal outcome, read from the
    ``enrollment_completed`` / ``enrollment_failed`` activities matched by
    enrollment id (outcomes are timeline-only, §V.15).
  - **contact_disabled** -- read from ``handled.json``'s per-scenario snapshot,
    taken right after that scenario was handled (the contact is re-enabled
    between scenarios, so a live read would be wrong).
  - **agent_replied** -- whether the agent sent a reply on the thread (a second
    outbound row on the ephemeral workflow, beyond Touch 1).
  - **follow-up task** -- a pending task on the ephemeral workflow (the
    ``contact_later`` re-enrollment or a soft follow-up).
  - **reply text** -- substring checks against the agent's reply body (e.g. the
    calendar link for the booked branch).

Expectations are tolerant of the wording-vs-tool gap by design: the gating keys
on the branch-defining signal, and a divergent outcome type is reported, not
gated, where the catalog uses ``"any"``. Writes ``verify.json`` and a verdict.

Usage:
    uv run python scripts/verify_branches.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json

from _common import (
    PROSPECT_EMAIL,
    SENDER_EMAIL,
    load_scenarios,
    mp,
    read_json,
    run_dir,
    write_json,
)


def _outcomes_by_enrollment(since: str) -> dict[str, str]:
    """Map enrollment id -> 'completed' | 'failed' from outcome activities."""
    outcomes: dict[str, str] = {}
    for outcome in ("failed", "completed"):
        listing = mp(
            [
                "activity",
                "list",
                "--contact-email",
                PROSPECT_EMAIL,
                "--type",
                f"enrollment_{outcome}",
                "--since",
                since,
                "--limit",
                "100",
            ],
            check=False,
        )
        for activity in listing.get("activities", []):
            enrollment_id = activity.get("enrollment_id")
            if enrollment_id:
                # 'completed' wins ties by being applied last.
                outcomes[enrollment_id] = outcome
    return outcomes


def _outbound_rows(workflow_id: str) -> list[dict]:
    """Return outbound email rows on a scenario's ephemeral workflow."""
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
            "20",
        ],
        check=False,
    )
    return listing.get("emails", [])


def _has_pending_task(workflow_id: str) -> bool:
    """True when the ephemeral workflow has a pending (future) task."""
    listing = mp(
        [
            "task",
            "list",
            "--workflow-id",
            workflow_id,
            "--status",
            "pending",
            "--limit",
            "20",
        ],
        check=False,
    )
    return bool(listing.get("tasks"))


def _reply_excerpt(rows: list[dict], touch1_email_id: str | None) -> str:
    """Return the body of the agent's reply (the outbound that is not Touch 1)."""
    for row in rows:
        if row["id"] == touch1_email_id:
            continue
        view = mp(["email", "view", row["id"]], check=False)
        if view.get("ok"):
            return str(view.get("email", {}).get("body_text", ""))
    return ""


def _outcome_note(
    exp_outcome: str, obs_outcome: str, followup_task: bool
) -> str | None:
    """Return a mismatch note for the terminal-outcome expectation, or None.

    ``terminal_or_followup`` passes on either a recorded outcome or a pending
    follow-up task; ``none`` requires no outcome; ``any`` never fails; a literal
    outcome (``completed`` / ``failed``) must match exactly.
    """
    if exp_outcome == "terminal_or_followup":
        if obs_outcome in ("completed", "failed") or followup_task:
            return None
        return "expected a terminal outcome or a follow-up task; found neither"
    if exp_outcome == "none" and obs_outcome != "none":
        return f"expected no terminal outcome; found {obs_outcome}"
    if exp_outcome not in ("any", "none") and obs_outcome != exp_outcome:
        return f"expected outcome {exp_outcome}; found {obs_outcome}"
    return None


def _evaluate(expect: dict, observed: dict) -> tuple[bool, list[str]]:
    """Compare observed state to the scenario expectations. Return (pass, notes)."""
    notes: list[str] = []

    outcome_note = _outcome_note(
        expect.get("outcome", "any"), observed["outcome"], observed["followup_task"]
    )
    if outcome_note:
        notes.append(outcome_note)

    exp_disabled = expect.get("contact_disabled")
    if exp_disabled is not None and observed["contact_disabled"] != exp_disabled:
        notes.append(
            f"expected contact_disabled={exp_disabled}; "
            f"found {observed['contact_disabled']}"
        )

    exp_replied = expect.get("agent_replied")
    if exp_replied is not None and observed["agent_replied"] != exp_replied:
        notes.append(
            f"expected agent_replied={exp_replied}; found {observed['agent_replied']}"
        )

    excerpt = (observed["reply_excerpt"] or "").lower()
    notes.extend(
        f"agent reply missing expected text: {needle!r}"
        for needle in expect.get("reply_contains", [])
        if needle.lower() not in excerpt
    )

    return not notes, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scaffold = read_json(directory / "scaffold.json")
    touch1 = read_json(directory / "touch1.json")
    handled = read_json(directory / "handled.json")

    entries_by_key = {e["scenario_key"]: e for e in scaffold["entries"]}
    touch1_by_key = {s["scenario_key"]: s for s in touch1["sends"]}
    handled_by_key = {h["scenario_key"]: h for h in handled["handled"]}
    catalog = {s["key"]: s for s in load_scenarios()}

    outcomes = _outcomes_by_enrollment(touch1["window_start"])

    results = []
    for scenario in load_scenarios():
        key = scenario["key"]
        entry = entries_by_key.get(key)
        handle = handled_by_key.get(key, {})
        if entry is None:
            results.append(
                {"scenario_key": key, "pass": False, "notes": ["scenario not set up"]}
            )
            continue

        workflow_id = entry["ephemeral_workflow_id"]
        enrollment_id = entry["enrollment_id"]
        rows = _outbound_rows(workflow_id)
        touch1_email_id = (touch1_by_key.get(key) or {}).get("outbound_email_id")

        observed = {
            "outcome": outcomes.get(enrollment_id, "none"),
            "contact_disabled": bool(handle.get("contact_disabled_after")),
            "disabled_reason": handle.get("disabled_reason_after"),
            "agent_replied": len(rows) >= 2,
            "followup_task": _has_pending_task(workflow_id),
            "reply_excerpt": _reply_excerpt(rows, touch1_email_id),
            "outbound_count": len(rows),
            "tool_calls": handle.get("tool_calls"),
        }

        handled_status = handle.get("handled_status")
        if handled_status != "handled":
            results.append(
                {
                    "scenario_key": key,
                    "label": scenario["label"],
                    "expected_branch": scenario["expected_branch"],
                    "pass": False,
                    "observed": observed,
                    "notes": [f"reply was not handled (status={handled_status})"],
                }
            )
            continue

        passed, notes = _evaluate(catalog[key]["expect"], observed)
        results.append(
            {
                "scenario_key": key,
                "label": scenario["label"],
                "expected_branch": scenario["expected_branch"],
                "expect": catalog[key]["expect"],
                "observed": observed,
                "pass": passed,
                "notes": notes,
            }
        )

    passed_count = sum(1 for r in results if r["pass"])
    verdict = "PASS" if passed_count == len(results) else "FAIL"
    write_json(
        directory / "verify.json",
        {
            "window_start": touch1["window_start"],
            "verdict": verdict,
            "passed": passed_count,
            "of": len(results),
            "scenarios": results,
        },
    )

    print(
        json.dumps(
            {
                "verdict": verdict,
                "passed": passed_count,
                "of": len(results),
                "failed": [r["scenario_key"] for r in results if not r["pass"]],
            },
            indent=2,
        )
    )
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
