"""Build the per-run scaffolding the agentic test drives.

For each selected real contact this:
  1. Ensures the neutral parking company and the alias-contact exist (idempotent).
  2. Mirrors the real contact's first/last name, title, and company onto the
     alias-contact (whose own email stays the inbound alias). The alias-contact
     is linked to the REAL company so the workflow's ``read_company`` step has
     real grounding; cleanup re-parks it on the neutral company afterward so the
     real company's contact_count is untouched at rest (§V.96).
  3. Imports an ephemeral, per-run copy of the workflow into the sender account
     (unique name -> fresh workflow id -> no 30-day cooldown on re-run, §V.79).
  4. Enrolls each alias-contact in that ephemeral workflow.

Writes ``scaffold.json`` so ``run_agents.py`` and ``cleanup.py`` know the
ephemeral workflow id, the enrollment ids, and which real company each
alias-contact was linked to. No mail is sent here.

Usage:
    uv run python scripts/setup_run.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from _common import (
    NEUTRAL_COMPANY_DOMAIN,
    NEUTRAL_COMPANY_NAME,
    SENDER_EMAIL,
    ephemeral_workflow_name,
    mp,
    read_json,
    run_dir,
    write_json,
)

_NAME_LINE_RE = re.compile(r'^name\s*=\s*".*"\s*$', re.MULTILINE)


def _ensure_neutral_company() -> None:
    """Create + disable the neutral parking company if absent (idempotent)."""
    existing = mp(["company", "view", NEUTRAL_COMPANY_DOMAIN], check=False)
    if not existing.get("ok"):
        mp(
            [
                "company",
                "create",
                "--domain",
                NEUTRAL_COMPANY_DOMAIN,
                "--name",
                NEUTRAL_COMPANY_NAME,
            ],
            check=False,
        )
    # Disable so it stays out of company list / discovery (§V.114). A
    # double-disable is a no-op error we ignore.
    mp(
        [
            "company",
            "disable",
            NEUTRAL_COMPANY_DOMAIN,
            "--reason",
            "campaign-test scaffolding",
        ],
        check=False,
    )


def _ensure_alias_contact(alias_email: str) -> str:
    """Create the alias-contact parked on the neutral company if absent.

    Returns the alias-contact's id.
    """
    existing = mp(["contact", "view", alias_email], check=False)
    if existing.get("ok"):
        return existing["contact"]["id"]
    created = mp(
        [
            "contact",
            "create",
            "--email",
            alias_email,
            "--company-domain",
            NEUTRAL_COMPANY_DOMAIN,
        ],
        check=False,
    )
    contact = created.get("contact") or {}
    if contact.get("id"):
        return contact["id"]
    # Fall back to a view in case create raced or returned a bare envelope.
    return mp(["contact", "view", alias_email], check=True)["contact"]["id"]


def _mirror(alias_email: str, real: dict) -> str:
    """Mirror the real contact's fields onto the alias-contact.

    Empty real fields reset to "" so stale data from a prior run never bleeds
    in. Links to the real company for grounding, or the neutral company when the
    real contact has none. Returns the company domain the alias-contact was
    linked to.
    """
    company_domain = real.get("company_domain") or NEUTRAL_COMPANY_DOMAIN
    update_args = [
        "contact",
        "update",
        alias_email,
        "--first-name",
        real.get("first_name") or "",
        "--last-name",
        real.get("last_name") or "",
        "--title",
        real.get("title") or "",
        "--company-domain",
        company_domain,
    ]
    mp(update_args, check=True)
    return company_domain


def _import_ephemeral_workflow(workflow_file: str, run_id: str, directory: Path) -> str:
    """Write + import a uniquely-named copy of the workflow. Return its id."""
    name = ephemeral_workflow_name(run_id)
    source = Path(workflow_file).read_text()
    renamed, count = _NAME_LINE_RE.subn(f'name = "{name}"', source, count=1)
    if count != 1:
        raise RuntimeError(f"could not find a top-level name line in {workflow_file}")
    ephemeral_path = directory / "ephemeral_workflow.toml"
    ephemeral_path.write_text(renamed)
    mp(
        [
            "workflow",
            "import",
            "--account-email",
            SENDER_EMAIL,
            "--file",
            str(ephemeral_path),
        ],
        check=True,
    )
    listing = mp(
        ["workflow", "list", "--account-email", SENDER_EMAIL, "--limit", "100"],
        check=True,
    )
    for workflow in listing.get("workflows", []):
        if workflow.get("name") == name:
            return workflow["id"]
    raise RuntimeError(f"imported workflow {name!r} not found in workflow list")


def _enroll(workflow_id: str, alias_email: str) -> str:
    """Enroll the alias-contact in the ephemeral workflow. Return enrollment id."""
    added = mp(
        [
            "enrollment",
            "add",
            "--workflow-id",
            workflow_id,
            "--contact-email",
            alias_email,
        ],
        check=False,
    )
    enrollment = added.get("enrollment") or {}
    if enrollment.get("id"):
        return enrollment["id"]
    listing = mp(
        [
            "enrollment",
            "list",
            "--workflow-id",
            workflow_id,
            "--contact-email",
            alias_email,
            "--limit",
            "5",
        ],
        check=True,
    )
    rows = listing.get("enrollments", [])
    if not rows:
        raise RuntimeError(f"could not enroll {alias_email} in {workflow_id}")
    return rows[0]["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    contacts = manifest["contacts"]

    _ensure_neutral_company()
    workflow_id = _import_ephemeral_workflow(
        manifest["workflow_file"], args.run_id, directory
    )

    entries = []
    for contact in contacts:
        alias_email = contact["alias"]
        alias_contact_id = _ensure_alias_contact(alias_email)
        linked_company = _mirror(alias_email, contact)
        enrollment_id = _enroll(workflow_id, alias_email)
        entries.append(
            {
                "sequence": contact["sequence"],
                "alias": alias_email,
                "alias_contact_id": alias_contact_id,
                "enrollment_id": enrollment_id,
                "linked_company_domain": linked_company,
                "real_contact_email": contact.get("email"),
            }
        )

    scaffold = {
        "ephemeral_workflow_id": workflow_id,
        "ephemeral_workflow_name": ephemeral_workflow_name(args.run_id),
        "neutral_company_domain": NEUTRAL_COMPANY_DOMAIN,
        "entries": entries,
    }
    write_json(directory / "scaffold.json", scaffold)

    print(
        json.dumps(
            {
                "ephemeral_workflow_id": workflow_id,
                "alias_contacts": len(entries),
                "enrolled": len(entries),
            },
            indent=2,
        )
    )
    return 0 if entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
