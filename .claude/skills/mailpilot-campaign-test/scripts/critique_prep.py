"""Bundle each sent email with its contact and company context for critique.

For every email that actually went out, joins the rendered subject and body with
the recipient's contact fields and their company profile (fetched once per
domain via ``mailpilot company view``). Writes ``critique_input.json`` -- one
tidy record per sent email -- so the Opus critique sub-agent reads a single file
instead of running many CLI queries. Deterministic data plumbing, no LLM.

Usage:
    uv run python scripts/critique_prep.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json

from _common import mp, read_json, run_dir, write_json

# Company-profile fields worth giving the critic for grounding (subset of
# CompanyProfile); kept small so the critique input stays compact.
PROFILE_FIELDS = ("summary", "products", "target_customers")


def _company_context(domain: str, cache: dict[str, dict]) -> dict:
    """Return {name, profile fields} for a company domain, cached per domain."""
    if domain in cache:
        return cache[domain]
    context: dict[str, object] = {"domain": domain}
    data = mp(["company", "view", domain], check=False)
    company = data.get("company") if isinstance(data, dict) else None
    if isinstance(company, dict):
        context["name"] = company.get("name")
        profile = company.get("profile") or {}
        if isinstance(profile, dict):
            for field in PROFILE_FIELDS:
                context[field] = profile.get(field)
    cache[domain] = context
    return context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    rendered = {
        e["sequence"]: e for e in read_json(directory / "personalized.json")["rendered"]
    }
    contact_by_seq = {c["sequence"]: c for c in manifest["contacts"]}
    sends = read_json(directory / "sends.json")["sends"]

    cache: dict[str, dict] = {}
    records = []
    for send in sends:
        if send.get("status") != "sent":
            continue
        seq = send["sequence"]
        contact = contact_by_seq.get(seq, {})
        entry = rendered.get(seq, {})
        domain = contact.get("company_domain")
        company = _company_context(domain, cache) if domain else None
        records.append(
            {
                "sequence": seq,
                "alias": send.get("alias"),
                "contact": {
                    "first_name": contact.get("first_name"),
                    "last_name": contact.get("last_name"),
                    "title": contact.get("title"),
                    "email": contact.get("email"),
                    "company_domain": domain,
                },
                "company": company,
                "subject": entry.get("subject"),
                "body": entry.get("body"),
            }
        )

    write_json(directory / "critique_input.json", {"emails": records})
    print(json.dumps({"prepared": len(records)}, indent=2))
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
