"""Select the real contacts that supply personalization data.

Pulls active contacts via ``mailpilot contact list`` and records up to nine as
the test set -- one per alias-contact. Each selected contact supplies real
fields (first name, title, company domain); a later step mirrors those onto a
persistent alias-contact whose own email is the inbound alias, so the agent
sends to the alias and never to the real address. The nine alias-contacts
themselves are excluded from selection so the test never runs against its own
scaffolding. Writes ``run_manifest.json`` combining the selected contacts with
the resolved state carried in ``preflight.json``.

Usage:
    uv run python scripts/select_contacts.py --run-id <id> \
        [--limit N] [--company-domain <domain>] [--min-confidence N]
"""

from __future__ import annotations

import argparse
import json

from _common import (
    ALIASES,
    MAX_ALIASES,
    NEUTRAL_COMPANY_DOMAIN,
    alias_for,
    mp,
    read_json,
    run_dir,
    write_json,
)

# Fields a real contact contributes to personalization (subset of
# ContactSummary). ``id``/``email`` identify the source row; the rest are
# mirrored onto the alias-contact.
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

    # One alias-contact per alias, so never select more than there are aliases.
    limit = min(args.limit, MAX_ALIASES)

    directory = run_dir(args.run_id)
    preflight = read_json(directory / "preflight.json")

    # Infrastructure addresses are never prospects: the system's own accounts
    # (outbound@/inbound@/hello@lab5.ca) and the nine alias-contacts.
    accounts = mp(
        ["account", "list", "--include-disabled", "--limit", "100"], check=False
    )
    excluded = {str(a.get("email", "")).lower() for a in accounts.get("accounts", [])}
    excluded.update(a.lower() for a in ALIASES)

    # Over-fetch so excluding scaffolding still leaves a full selection.
    list_args = ["contact", "list", "--limit", str(limit + 2 * MAX_ALIASES)]
    if args.company_domain is not None:
        list_args += ["--company-domain", args.company_domain]
    if args.min_confidence is not None:
        list_args += ["--min-email-confidence", str(args.min_confidence)]
    data = mp(list_args, check=False)

    contacts = []
    sequence = 0
    for row in data.get("contacts", []):
        # Never select the system accounts, alias-contacts, or neutral test rows.
        if str(row.get("email", "")).lower() in excluded:
            continue
        if row.get("company_domain") == NEUTRAL_COMPANY_DOMAIN:
            continue
        sequence += 1
        if sequence > limit:
            break
        contact = {field: row.get(field) for field in CONTACT_FIELDS}
        contact["sequence"] = sequence
        contact["alias"] = alias_for(sequence)
        contacts.append(contact)

    manifest = {
        "run_id": args.run_id,
        "sender_account_id": preflight["sender_account_id"],
        "alias_mailbox": preflight["alias_mailbox"],
        "workflow_file": preflight["workflow_file"],
        "workflow_name": preflight.get("workflow_name"),
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
