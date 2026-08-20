"""Preflight: verify the environment can run the multi-step campaign test.

Validates the outbound workflow TOML (the agent definition under test),
resolves the sender (outbound@lab5.ca) and the prospect mailbox
(inbound@lab5.ca, which is also the prospect contact's address), confirms
neither account is disabled, confirms Google credentials are configured (the
live send needs them), requires ``environment == "dev"`` (§V.165),
and counts the real contacts available as Touch 1 personalization grounding.
Writes ``preflight.json`` (the single source of resolved state for later
scripts) and exits non-zero on a blocking issue.

Usage:
    uv run python scripts/preflight.py --run-id <id> [--workflow-file <path>]
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from _common import (
    DEFAULT_WORKFLOW_FILE,
    PROSPECT_EMAIL,
    PROSPECT_MAILBOX,
    REQUIRED_OUTBOUND_DISPLAY_NAME,
    REQUIRED_OUTBOUND_SIGNATURE,
    SENDER_EMAIL,
    mp,
    repo_root,
    resolve_account,
    run_dir,
    write_json,
)

_REQUIRED_WORKFLOW_FIELDS = ("name", "template", "goal", "instructions")
REQUIRED_ENVIRONMENT = "dev"


def _check_environment(
    environment: object, result: dict[str, object], issues: list[str]
) -> None:
    """Block unless settings.environment is dev (§V.165 / §V.176)."""
    result["environment"] = environment
    if environment == "dev":
        result["logfire_environment"] = "development"
    elif environment == "prd":
        result["logfire_environment"] = "production"
    else:
        result["logfire_environment"] = None
    ok = environment == REQUIRED_ENVIRONMENT
    result["environment_ok"] = ok
    if not ok:
        issues.append(
            f"environment={environment!r} "
            f"(want {REQUIRED_ENVIRONMENT!r}); "
            "live campaign-test runs only in dev — restore DEV config "
            "before any account create/update or send"
        )


def _resolve_workflow(
    workflow_file: str, result: dict[str, object], issues: list[str]
) -> None:
    path = Path(workflow_file)
    if not path.is_absolute():
        path = repo_root() / workflow_file
    result["workflow_file"] = str(path)
    if not path.is_file():
        issues.append(
            f"workflow file not found: {workflow_file} "
            "(is the workflows/ symlink present? `ln -s ../workflows workflows`)"
        )
        return
    try:
        parsed = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        issues.append(f"workflow file is not valid TOML: {exc}")
        return
    missing = [f for f in _REQUIRED_WORKFLOW_FIELDS if not parsed.get(f)]
    if missing:
        issues.append(f"workflow file missing required field(s): {', '.join(missing)}")
        return
    result["workflow_name"] = parsed["name"]
    result["workflow_template"] = parsed["template"]
    if not str(parsed["template"]).startswith("outbound"):
        issues.append(
            f"WARNING workflow template {parsed['template']!r} is not an outbound "
            "template; the campaign test drives an outbound reach-out plus replies"
        )


def _check_outbound_signature(
    sender: dict[str, object] | None, result: dict[str, object], issues: list[str]
) -> None:
    """Block when outbound@ signature missing or mismatches §V.151 required fields.

    Skill step 0c sets the fields via ``account create|update``; preflight only
    verifies. Nested ``signature`` keys match AccountSignature projection.
    """
    required = REQUIRED_OUTBOUND_SIGNATURE
    result["required_outbound_signature"] = dict(required)
    if not sender:
        result["outbound_signature"] = None
        result["outbound_signature_ok"] = False
        return
    sig = sender.get("signature")
    result["outbound_signature"] = sig if isinstance(sig, dict) else None
    if not isinstance(sig, dict):
        issues.append(
            f"sending account {SENDER_EMAIL} signature missing; set via "
            "`mailpilot account update` --signature-full-name/title/website/phone "
            "(§V.151); campaign-test requires exact lab5 signature"
        )
        result["outbound_signature_ok"] = False
        return
    mismatches: list[str] = []
    for key, expected in required.items():
        actual = sig.get(key)
        if actual != expected:
            mismatches.append(f"{key}={actual!r} (want {expected!r})")
    if mismatches:
        issues.append(
            f"sending account {SENDER_EMAIL} signature mismatch: "
            + "; ".join(mismatches)
            + " — re-run skill step 0c (account update signature flags)"
        )
        result["outbound_signature_ok"] = False
    else:
        result["outbound_signature_ok"] = True


def _check_outbound_display_name(
    sender: dict[str, object] | None, result: dict[str, object], issues: list[str]
) -> None:
    """Block when outbound@ From display_name mismatches required identity.

    ``display_name`` is the From-header name (§V.151), distinct from
    signature.full_name. Skill step 0c sets both; preflight verifies both.
    """
    required = REQUIRED_OUTBOUND_DISPLAY_NAME
    result["required_outbound_display_name"] = required
    if not sender:
        result["outbound_display_name"] = None
        result["outbound_display_name_ok"] = False
        return
    actual = sender.get("display_name")
    result["outbound_display_name"] = actual
    if actual != required:
        issues.append(
            f"sending account {SENDER_EMAIL} display_name={actual!r} "
            f"(want {required!r}); set via `mailpilot account update "
            f"--display-name {required!r}` — re-run skill step 0c"
        )
        result["outbound_display_name_ok"] = False
    else:
        result["outbound_display_name_ok"] = True


def _resolve_accounts(result: dict[str, object], issues: list[str]) -> None:
    sender = resolve_account(SENDER_EMAIL)
    mailbox = resolve_account(PROSPECT_MAILBOX)
    result["sender_account_id"] = sender.get("id") if sender else None
    result["prospect_mailbox_account_id"] = mailbox.get("id") if mailbox else None
    if not sender:
        issues.append(
            f"sending account {SENDER_EMAIL} not found (run `mailpilot account create`)"
        )
    elif sender.get("disabled_reason"):
        issues.append(
            f"sending account {SENDER_EMAIL} is disabled "
            f"({sender['disabled_reason']}); send and reply are blocked (§V.79)"
        )
    _check_outbound_signature(sender, result, issues)
    _check_outbound_display_name(sender, result, issues)
    if not mailbox:
        issues.append(
            f"prospect mailbox {PROSPECT_MAILBOX} not found "
            "(run `mailpilot account create`)"
        )
    elif mailbox.get("disabled_reason"):
        issues.append(
            f"prospect mailbox {PROSPECT_MAILBOX} is disabled "
            f"({mailbox['disabled_reason']}); replies cannot be sent or confirmed"
        )


def _count_contacts(result: dict[str, object], issues: list[str]) -> None:
    """Count real contacts available for Touch 1 grounding.

    Infrastructure addresses are never grounding prospects: the system's own
    accounts (which includes the prospect mailbox address).
    """
    accounts = mp(
        ["account", "list", "--include-disabled", "--limit", "100"], check=False
    )
    excluded = {str(a.get("email", "")).lower() for a in accounts.get("accounts", [])}
    excluded.add(PROSPECT_EMAIL.lower())
    data = mp(["contact", "list", "--limit", "200"], check=False)
    real = [
        c
        for c in data.get("contacts", [])
        if str(c.get("email", "")).lower() not in excluded
    ]
    result["discovered_contact_count"] = len(real)
    if not real:
        issues.append(
            "no real contacts found (run `/lead-contacts` first to seed verified "
            "contact rows for Touch 1 grounding)"
        )


def _check_settings(result: dict[str, object], issues: list[str]) -> None:
    environment: object = None
    try:
        sys.path.insert(0, str(repo_root() / "src"))
        from mailpilot.settings import get_settings

        settings = get_settings()
        google_creds = bool(settings.google_application_credentials)
        environment = settings.environment
    except Exception as exc:
        google_creds = False
        issues.append(f"could not load settings: {exc}")
    result["google_credentials_configured"] = google_creds
    _check_environment(environment, result, issues)
    if not google_creds:
        issues.append(
            "google_application_credentials not set (live Gmail send cannot run)"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow-file", default=DEFAULT_WORKFLOW_FILE)
    args = parser.parse_args()

    issues: list[str] = []
    result: dict[str, object] = {
        "sender_email": SENDER_EMAIL,
        "prospect_mailbox": PROSPECT_MAILBOX,
        "prospect_email": PROSPECT_EMAIL,
    }

    _check_settings(result, issues)
    if result.get("environment_ok") is True:
        _resolve_workflow(args.workflow_file, result, issues)
        _resolve_accounts(result, issues)
        _count_contacts(result, issues)

    blocking = [i for i in issues if "WARNING" not in i]
    result["issues"] = issues
    result["verdict"] = "ok" if not blocking else "fail"

    write_json(run_dir(args.run_id) / "preflight.json", result)
    print(json.dumps({"verdict": result["verdict"], "issues": issues}, indent=2))
    return 0 if not blocking else 1


if __name__ == "__main__":
    raise SystemExit(main())
