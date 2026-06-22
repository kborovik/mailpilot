"""Preflight: verify the environment can run the live campaign test.

Parses the campaign Markdown file, resolves the sender (outbound@lab5.ca) and
the alias mailbox (inbound@lab5.ca, which receives every inbound{1-9} alias),
confirms Google credentials are configured (the live send needs them), notes
whether the alias mailbox carries an active workflow, and counts the discovered
contacts available as personalization data. Writes ``preflight.json`` (the
single source of resolved state for later scripts) and exits non-zero on a
blocking issue.

Usage:
    uv run python scripts/preflight.py --run-id <id> --campaign <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _common import (
    ALIAS_MAILBOX,
    ALIASES,
    SENDER_EMAIL,
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
    mailbox_id = resolve_account_id(ALIAS_MAILBOX)
    result["sender_account_id"] = sender_id
    result["alias_mailbox_account_id"] = mailbox_id
    if not sender_id:
        issues.append(
            f"sending account {SENDER_EMAIL} not found (run `mailpilot account create`)"
        )
    if not mailbox_id:
        issues.append(
            f"alias mailbox {ALIAS_MAILBOX} not found (run `mailpilot account create`)"
        )


def _note_mailbox_workflows(result: dict[str, object], issues: list[str]) -> None:
    """Note active workflows on the alias mailbox; never blocking.

    The alias mailbox normally carries the inbound demo workflow. No auto-reply
    fires because the skill never starts ``mailpilot run``.
    """
    mailbox_id = result.get("alias_mailbox_account_id")
    if not isinstance(mailbox_id, str):
        return
    data = mp(["workflow", "list", "--account-email", mailbox_id], check=False)
    active = [w for w in data.get("workflows", []) if w.get("status") == "active"]
    result["alias_mailbox_active_workflows"] = len(active)
    if active:
        issues.append(
            f"WARNING alias mailbox {ALIAS_MAILBOX} has {len(active)} active "
            "workflow(s); the skill does not start `mailpilot run`, so no "
            "auto-reply fires -- do not start the run loop during the test"
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
        "alias_mailbox": ALIAS_MAILBOX,
        "aliases": ALIASES,
    }

    _resolve_campaign(args.campaign, result, issues)
    _resolve_accounts(result, issues)
    _note_mailbox_workflows(result, issues)
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
