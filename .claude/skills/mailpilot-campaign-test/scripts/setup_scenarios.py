"""Build the per-run, per-scenario scaffolding the multi-step test drives.

For the run this:
  1. Ensures the neutral parking company and the single prospect contact exist
     (idempotent). The prospect contact's own email is ``inbound@lab5.ca`` -- the
     controlled mailbox -- so the agent can only ever email the test mailbox.
  2. Mirrors the selected real contact's first/last name and title onto the
     prospect contact and links it to the REAL grounding company, so the
     workflow's ``read_contact`` / ``read_company`` steps have real grounding.
     Cleanup re-parks it on the neutral company afterward (§V.96). Re-enables the
     prospect contact if a prior interrupted run left it disabled.
  3. For each scenario, imports an ephemeral, uniquely-named copy of the workflow
     into the sender account and enrolls the prospect contact in it. A fresh
     workflow id per scenario gives each scenario an independent enrollment and
     dodges the 30-day cold-send cooldown (§V.79).

Writes ``scaffold.json`` so the send/handle/cleanup steps know each scenario's
ephemeral workflow id and enrollment id, and which real company the prospect
contact was linked to. No mail is sent here.

Usage:
    uv run python scripts/setup_scenarios.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from _common import (
    NEUTRAL_COMPANY_DOMAIN,
    NEUTRAL_COMPANY_NAME,
    PROSPECT_EMAIL,
    SENDER_EMAIL,
    ephemeral_workflow_name,
    load_scenarios,
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


def _ensure_prospect_contact() -> str:
    """Create the prospect contact parked on the neutral company if absent.

    Re-enables it if a prior interrupted run's opt-out / wrong-person branch left
    it disabled, so this run starts from a clean, enabled contact. Returns the
    prospect contact's id.
    """
    existing = mp(["contact", "view", PROSPECT_EMAIL], check=False)
    if existing.get("ok"):
        if existing["contact"].get("disabled_reason"):
            mp(["contact", "enable", PROSPECT_EMAIL], check=False)
        return existing["contact"]["id"]
    created = mp(
        [
            "contact",
            "create",
            "--email",
            PROSPECT_EMAIL,
            "--company-domain",
            NEUTRAL_COMPANY_DOMAIN,
        ],
        check=False,
    )
    contact = created.get("contact") or {}
    if contact.get("id"):
        return contact["id"]
    return mp(["contact", "view", PROSPECT_EMAIL], check=True)["contact"]["id"]


def _mirror(grounding: dict | None) -> str:
    """Mirror the grounding contact's identity onto the prospect contact.

    Empty fields reset to "" so stale data from a prior run never bleeds in.
    Links to the real grounding company, or the neutral company when there is no
    grounding contact. Returns the company domain the prospect contact is linked
    to.
    """
    grounding = grounding or {}
    company_domain = grounding.get("company_domain") or NEUTRAL_COMPANY_DOMAIN
    mp(
        [
            "contact",
            "update",
            PROSPECT_EMAIL,
            "--first-name",
            grounding.get("first_name") or "",
            "--last-name",
            grounding.get("last_name") or "",
            "--title",
            grounding.get("title") or "",
            "--company-domain",
            company_domain,
        ],
        check=True,
    )
    return company_domain


def _import_ephemeral_workflow(
    source_text: str, run_id: str, scenario_key: str, directory: Path
) -> str:
    """Write + import a uniquely-named copy of the workflow. Return its id."""
    name = ephemeral_workflow_name(run_id, scenario_key)
    renamed, count = _NAME_LINE_RE.subn(f'name = "{name}"', source_text, count=1)
    if count != 1:
        raise RuntimeError("could not find a top-level name line in the workflow file")
    ephemeral_path = directory / f"ephemeral_{scenario_key}.toml"
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
        ["workflow", "list", "--account-email", SENDER_EMAIL, "--limit", "200"],
        check=True,
    )
    for workflow in listing.get("workflows", []):
        if workflow.get("name") == name:
            return workflow["id"]
    raise RuntimeError(f"imported workflow {name!r} not found in workflow list")


def _enroll(workflow_id: str, contact_email: str) -> str:
    """Enroll the prospect contact in the ephemeral workflow. Return its id."""
    added = mp(
        [
            "enrollment",
            "add",
            "--workflow-id",
            workflow_id,
            "--contact-email",
            contact_email,
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
            contact_email,
            "--limit",
            "5",
        ],
        check=True,
    )
    rows = listing.get("enrollments", [])
    if not rows:
        raise RuntimeError(f"could not enroll {contact_email} in {workflow_id}")
    return rows[0]["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    grounding = manifest.get("grounding_contact")
    source_text = Path(manifest["workflow_file"]).read_text()

    _ensure_neutral_company()
    prospect_contact_id = _ensure_prospect_contact()
    linked_company = _mirror(grounding)

    entries = []
    for scenario in load_scenarios():
        key = scenario["key"]
        workflow_id = _import_ephemeral_workflow(
            source_text, args.run_id, key, directory
        )
        enrollment_id = _enroll(workflow_id, PROSPECT_EMAIL)
        entries.append(
            {
                "scenario_key": key,
                "ephemeral_workflow_id": workflow_id,
                "ephemeral_workflow_name": ephemeral_workflow_name(args.run_id, key),
                "enrollment_id": enrollment_id,
            }
        )

    scaffold = {
        "prospect_contact_id": prospect_contact_id,
        "prospect_email": PROSPECT_EMAIL,
        "neutral_company_domain": NEUTRAL_COMPANY_DOMAIN,
        "linked_company_domain": linked_company,
        "entries": entries,
    }
    write_json(directory / "scaffold.json", scaffold)

    print(
        json.dumps(
            {
                "prospect_contact_id": prospect_contact_id,
                "linked_company_domain": linked_company,
                "scenarios_enrolled": len(entries),
            },
            indent=2,
        )
    )
    return 0 if entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
