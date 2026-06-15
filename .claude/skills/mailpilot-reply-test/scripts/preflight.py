"""Preflight: verify the environment can run the live reply test.

Resolves account ids, ensures the demo workflow is active on inbound (importing
it if needed), reads the Drive folder id from the workflow instructions, and
checks that the Anthropic key / Google credentials / Logfire token are present.
Writes ``preflight.json`` (the single source of resolved ids for later scripts)
and exits non-zero when a blocking issue is found.

Usage:
    uv run python scripts/preflight.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from _common import (
    DEFAULT_DRIVE_FOLDER_ID,
    INBOUND_EMAIL,
    OUTBOUND_EMAIL,
    mp,
    repo_root,
    resolve_account_id,
    run_dir,
    write_json,
)

WORKFLOW_TEMPLATE = "inbound-google-drive"
WORKFLOW_TOML = "workflows/demo-lab5-mailpilot.toml"
DRIVE_ID_RE = re.compile(r"\b([A-Za-z0-9_-]{25,})\b")


def _find_active_workflow(inbound_account_id: str) -> dict[str, str] | None:
    data = mp(["workflow", "list", "--account-id", inbound_account_id], check=False)
    for workflow in data.get("workflows", []):
        if workflow.get("template") == WORKFLOW_TEMPLATE:
            return workflow
    return None


def _resolve_accounts(result: dict[str, object], issues: list[str]) -> None:
    outbound_id = resolve_account_id(OUTBOUND_EMAIL)
    inbound_id = resolve_account_id(INBOUND_EMAIL)
    result["outbound_account_id"] = outbound_id
    result["inbound_account_id"] = inbound_id
    if not outbound_id:
        issues.append(
            f"sending account {OUTBOUND_EMAIL} not found (run `mailpilot account create`)"
        )
    if not inbound_id:
        issues.append(
            f"reply account {INBOUND_EMAIL} not found (run `mailpilot account create`)"
        )


def _resolve_workflow(result: dict[str, object], issues: list[str]) -> None:
    inbound_id = result.get("inbound_account_id")
    workflow = (
        _find_active_workflow(inbound_id) if isinstance(inbound_id, str) else None
    )
    if workflow is None and isinstance(inbound_id, str):
        # Idempotent upsert keyed on (account_id, name); auto-activates.
        mp(
            ["workflow", "import", "--account-id", inbound_id, "--file", WORKFLOW_TOML],
            check=False,
        )
        workflow = _find_active_workflow(inbound_id)
    if workflow is None:
        issues.append(
            f"no {WORKFLOW_TEMPLATE} workflow on {INBOUND_EMAIL} (import {WORKFLOW_TOML})"
        )
    else:
        result["workflow_id"] = workflow.get("id")
        result["workflow_status"] = workflow.get("status")
        if workflow.get("status") != "active":
            issues.append(
                f"demo workflow status is {workflow.get('status')!r}, expected 'active'"
            )

    # Prefer the Drive folder id embedded in the active workflow's instructions
    # so we validate the agent's real KB target; fall back to the demo folder.
    drive_folder_id = DEFAULT_DRIVE_FOLDER_ID
    workflow_id = result.get("workflow_id")
    if isinstance(workflow_id, str):
        view = mp(["workflow", "view", workflow_id], check=False)
        instructions = str(view.get("workflow", {}).get("instructions", ""))
        for candidate in DRIVE_ID_RE.findall(instructions):
            if candidate != WORKFLOW_TEMPLATE:
                drive_folder_id = candidate
                break
    result["drive_folder_id"] = drive_folder_id


def _check_settings(result: dict[str, object], issues: list[str]) -> None:
    try:
        sys.path.insert(0, str(repo_root() / "src"))
        from mailpilot.settings import get_settings

        settings = get_settings()
        anthropic_ok = bool(settings.anthropic_api_key)
        google_creds = bool(settings.google_application_credentials)
        logfire_ok = bool(settings.logfire_token)
        result["logfire_environment"] = settings.logfire_environment
    except Exception as exc:
        anthropic_ok = google_creds = logfire_ok = False
        result["logfire_environment"] = "development"
        issues.append(f"could not load settings: {exc}")

    result["anthropic_ok"] = anthropic_ok
    result["google_credentials_configured"] = google_creds
    result["logfire_ok"] = logfire_ok
    if not anthropic_ok:
        issues.append("anthropic_api_key not set (agent + classifier cannot run)")
    if not logfire_ok:
        issues.append(
            "logfire_token not set: WARNING token/latency analysis will be unavailable"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    issues: list[str] = []
    result: dict[str, object] = {
        "outbound_email": OUTBOUND_EMAIL,
        "inbound_email": INBOUND_EMAIL,
    }

    _resolve_accounts(result, issues)
    _resolve_workflow(result, issues)
    _check_settings(result, issues)

    # A missing Logfire token is a warning, not a blocker.
    blocking = [i for i in issues if "WARNING" not in i]
    result["issues"] = issues
    result["verdict"] = "ok" if not blocking else "fail"

    write_json(run_dir(args.run_id) / "preflight.json", result)
    print(json.dumps({"verdict": result["verdict"], "issues": issues}, indent=2))
    return 0 if not blocking else 1


if __name__ == "__main__":
    raise SystemExit(main())
