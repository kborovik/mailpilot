"""Bundle the workflow wording with the emails it produced for critique.

The critique judges the workflow's ``objective`` and ``instructions`` -- the
wording that drove the agent -- and the sent emails are evidence of what that
wording produces. This script writes ``critique_input.json`` with two parts: the
``workflow`` block (name, objective, instructions, read from the ephemeral
workflow TOML the run actually ran on) and the ``emails`` list (one record per
sent email, joining the agent-written subject and body with the real contact's
mirrored fields and the real company's profile, fetched once per domain via
``mailpilot company view``). The Opus critique sub-agent reads this single file
instead of running many CLI queries. Deterministic data plumbing, no LLM.

Usage:
    uv run python scripts/critique_prep.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

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


def _workflow_wording(directory: Path) -> dict[str, object]:
    """Return the run's workflow wording: name, objective, instructions.

    Reads the ephemeral workflow TOML the run actually imported, so the critic
    judges the exact wording the agent ran on (the ephemeral copy differs from
    the source only in its name line). Missing or unparseable TOML yields a
    sparse block so the critique still runs against the email evidence.
    """
    toml_path = directory / "ephemeral_workflow.toml"
    if not toml_path.exists():
        return {}
    try:
        data = tomllib.loads(toml_path.read_text())
    except tomllib.TOMLDecodeError:
        return {}
    return {
        "name": data.get("name"),
        "objective": data.get("objective"),
        "instructions": data.get("instructions"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    contact_by_seq = {c["sequence"]: c for c in manifest["contacts"]}
    sends = read_json(directory / "sends.json")["sends"]

    cache: dict[str, dict] = {}
    records = []
    for send in sends:
        if send.get("status") != "sent":
            continue
        seq = send["sequence"]
        contact = contact_by_seq.get(seq, {})
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
                "subject": send.get("subject"),
                "body": send.get("body"),
            }
        )

    bundle = {"workflow": _workflow_wording(directory), "emails": records}
    write_json(directory / "critique_input.json", bundle)
    print(json.dumps({"prepared": len(records)}, indent=2))
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
