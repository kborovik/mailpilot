"""Preflight: verify the environment can run the live campaign test.

Parses the campaign Markdown file, resolves the sender and sink account ids,
confirms Google credentials are configured (the live send needs them), confirms
the sink mailbox carries no active workflow (so no auto-reply fires), and counts
the discovered contacts available as personalization data. Writes
``preflight.json`` (the single source of resolved state for later scripts) and
exits non-zero on a blocking issue.

Usage:
    uv run python scripts/preflight.py --run-id <id> --campaign <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import (
    SENDER_EMAIL,
    SINK_EMAIL,
    mp,
    parse_campaign,
    repo_root,
    resolve_account_id,
    run_dir,
    write_json,
)


def _resolve_campaign(
    campaign_path: str, result: dict[str, object], issues: list[str]
) -> None:
    path = Path(campaign_path)
    result["campaign_path"] = str(path)
    if not path.is_file():
        issues.append(f"campaign file not found: {campaign_path}")
        return
    try:
        parsed = parse_campaign(path)
    except ValueError as exc:
        issues.append(f"campaign file invalid: {exc}")
        return
    result["subject_template"] = parsed["subject_template"]
    result["body_template"] = parsed["body_template"]


def _resolve_accounts(result: dict[str, object], issues: list[str]) -> None:
    sender_id = resolve_account_id(SENDER_EMAIL)
    sink_id = resolve_account_id(SINK_EMAIL)
    result["sender_account_id"] = sender_id
    result["sink_account_id"] = sink_id
    if not sender_id:
        issues.append(
            f"sending account {SENDER_EMAIL} not found (run `mailpilot account create`)"
        )
    if not sink_id:
        issues.append(
            f"sink account {SINK_EMAIL} not found (run `mailpilot account create`)"
        )


def _check_sink_quiet(result: dict[str, object], issues: list[str]) -> None:
    """Confirm the sink carries no active workflow (no auto-reply on delivery)."""
    sink_id = result.get("sink_account_id")
    if not isinstance(sink_id, str):
        return
    data = mp(["workflow", "list", "--account-email", sink_id], check=False)
    active = [w for w in data.get("workflows", []) if w.get("status") == "active"]
    result["sink_active_workflows"] = len(active)
    if active:
        issues.append(
            f"WARNING sink {SINK_EMAIL} has {len(active)} active workflow(s); a live "
            "`mailpilot run` could auto-reply to the test message"
        )


def _count_contacts(result: dict[str, object], issues: list[str]) -> None:
    data = mp(["contact", "list", "--limit", "100"], check=False)
    contacts = data.get("contacts", [])
    result["discovered_contact_count"] = len(contacts)
    if not contacts:
        issues.append(
            "no discovered contacts found (run `/lead-contacts` first to seed "
            "verified contact rows)"
        )


def _check_settings(result: dict[str, object], issues: list[str]) -> None:
    try:
        sys.path.insert(0, str(repo_root() / "src"))
        from mailpilot.settings import get_settings

        settings = get_settings()
        google_creds = bool(settings.google_application_credentials)
    except Exception as exc:
        google_creds = False
        issues.append(f"could not load settings: {exc}")
    result["google_credentials_configured"] = google_creds
    if not google_creds:
        issues.append(
            "google_application_credentials not set (live Gmail send cannot run)"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--campaign", required=True)
    args = parser.parse_args()

    issues: list[str] = []
    result: dict[str, object] = {
        "sender_email": SENDER_EMAIL,
        "sink_email": SINK_EMAIL,
    }

    _resolve_campaign(args.campaign, result, issues)
    _resolve_accounts(result, issues)
    _check_sink_quiet(result, issues)
    _count_contacts(result, issues)
    _check_settings(result, issues)

    blocking = [i for i in issues if "WARNING" not in i]
    result["issues"] = issues
    result["verdict"] = "ok" if not blocking else "fail"

    write_json(run_dir(args.run_id) / "preflight.json", result)
    print(json.dumps({"verdict": result["verdict"], "issues": issues}, indent=2))
    return 0 if not blocking else 1


if __name__ == "__main__":
    raise SystemExit(main())
