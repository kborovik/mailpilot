"""Select the one real contact that supplies Touch 1 personalization grounding.

The multi-step test sends Touch 1 to a single prospect contact whose own email
is ``inbound@lab5.ca``. That prospect contact carries no real identity of its
own, so a later step mirrors a real contact's first name, last name, title, and
company onto it -- enough for the agent's ``read_contact`` / ``read_company``
grounding to be real -- while the recipient stays the controlled mailbox. This
script picks that one grounding contact (highest-confidence first) and writes
``run_manifest.json``, combining it with the resolved state from
``preflight.json`` and the scenario catalog keys.

Usage:
    uv run python scripts/select_contacts.py --run-id <id> \
        [--company-domain <domain>] [--min-confidence N]
"""

from __future__ import annotations

import argparse
import json

from _common import (
    NEUTRAL_COMPANY_DOMAIN,
    PROSPECT_EMAIL,
    load_scenarios,
    mp,
    read_json,
    run_dir,
    write_json,
)

# Fields the grounding contact contributes (subset of ContactSummary).
# ``id``/``email`` identify the source row; the rest are mirrored onto the
# prospect contact.
CONTACT_FIELDS = (
    "id",
    "email",
    "first_name",
    "last_name",
    "title",
    "company_domain",
    "email_confidence",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--company-domain", default=None)
    parser.add_argument("--min-confidence", type=int, default=None)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    preflight = read_json(directory / "preflight.json")

    # Infrastructure addresses are never grounding prospects: the system's own
    # accounts (which includes the prospect mailbox address).
    accounts = mp(
        ["account", "list", "--include-disabled", "--limit", "100"], check=False
    )
    excluded = {str(a.get("email", "")).lower() for a in accounts.get("accounts", [])}
    excluded.add(PROSPECT_EMAIL.lower())

    list_args = ["contact", "list", "--limit", "50"]
    if args.company_domain is not None:
        list_args += ["--company-domain", args.company_domain]
    if args.min_confidence is not None:
        list_args += ["--min-email-confidence", str(args.min_confidence)]
    data = mp(list_args, check=False)

    grounding = None
    for row in data.get("contacts", []):
        if str(row.get("email", "")).lower() in excluded:
            continue
        if row.get("company_domain") == NEUTRAL_COMPANY_DOMAIN:
            continue
        grounding = {field: row.get(field) for field in CONTACT_FIELDS}
        break

    scenarios = [
        {
            "key": scenario["key"],
            "label": scenario["label"],
            "expected_branch": scenario["expected_branch"],
        }
        for scenario in load_scenarios()
    ]

    manifest = {
        "run_id": args.run_id,
        "sender_account_id": preflight["sender_account_id"],
        "prospect_email": preflight["prospect_email"],
        "prospect_mailbox": preflight["prospect_mailbox"],
        "workflow_file": preflight["workflow_file"],
        "workflow_name": preflight.get("workflow_name"),
        "grounding_contact": grounding,
        "scenarios": scenarios,
    }
    write_json(directory / "run_manifest.json", manifest)

    print(
        json.dumps(
            {
                "grounding_contact": grounding["email"] if grounding else None,
                "company_domain": grounding["company_domain"] if grounding else None,
                "scenarios": [s["key"] for s in scenarios],
            },
            indent=2,
        )
    )
    return 0 if grounding else 1


if __name__ == "__main__":
    raise SystemExit(main())
