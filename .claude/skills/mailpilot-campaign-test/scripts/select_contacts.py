"""Select the real contacts that supply personalization data.

Pulls active contacts via ``mailpilot contact list`` and records up to nine as
the test set -- one per inbound alias. Each contact supplies real fields (first
name, title, company domain, even its real email), but the send recipient is the
contact's assigned alias (inbound{1-9}@lab5.ca), never the contact's own
address. This simulates a campaign against real data while keeping every message
inside the test mailbox. Writes ``run_manifest.json`` combining the selected
contacts with the campaign templates carried in ``preflight.json``.

Usage:
    uv run python scripts/select_contacts.py --run-id <id> \
        [--limit N] [--company-domain <domain>] [--min-confidence N]
"""

from __future__ import annotations

import argparse
import json

from _common import MAX_ALIASES, alias_for, mp, read_json, run_dir, write_json

# Fields a contact contributes to personalization (subset of ContactSummary).
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
    parser.add_argument("--limit", type=int, default=MAX_ALIASES)
    parser.add_argument("--company-domain", default=None)
    parser.add_argument("--min-confidence", type=int, default=None)
    args = parser.parse_args()

    # One send per alias, so never select more contacts than there are aliases.
    limit = min(args.limit, MAX_ALIASES)

    directory = run_dir(args.run_id)
    preflight = read_json(directory / "preflight.json")

    list_args = ["contact", "list", "--limit", str(limit)]
    if args.company_domain is not None:
        list_args += ["--company-domain", args.company_domain]
    if args.min_confidence is not None:
        list_args += ["--min-email-confidence", str(args.min_confidence)]
    data = mp(list_args, check=False)

    contacts = []
    for sequence, row in enumerate(data.get("contacts", [])[:limit], start=1):
        contact = {field: row.get(field) for field in CONTACT_FIELDS}
        contact["sequence"] = sequence
        contact["alias"] = alias_for(sequence)
        contacts.append(contact)

    manifest = {
        "run_id": args.run_id,
        "sender_account_id": preflight["sender_account_id"],
        "alias_mailbox": preflight["alias_mailbox"],
        "subject_template": preflight["subject_template"],
        "body_template": preflight["body_template"],
        "contacts": contacts,
    }
    write_json(directory / "run_manifest.json", manifest)

    print(
        json.dumps(
            {
                "selected": len(contacts),
                "limit": limit,
                "company_domain": args.company_domain,
                "min_confidence": args.min_confidence,
            },
            indent=2,
        )
    )
    return 0 if contacts else 1


if __name__ == "__main__":
    raise SystemExit(main())
