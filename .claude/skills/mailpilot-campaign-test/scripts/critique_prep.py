"""Bundle the workflow wording with the reply branches it produced for critique.

The critique judges the workflow's ``goal`` and ``instructions`` -- in
particular its "Handling replies" section -- and the agent's actual branch
behavior is the evidence. This script writes ``critique_input.json`` with two
parts: the ``workflow`` block (name, goal, instructions, read from the
source workflow TOML the run imported) and a ``scenarios`` list. Each scenario
record carries the crafted inbound reply, the agent's handling reply, the branch
the agent was expected to take, and the observed outcome (whether it took it).
The Opus critique sub-agent reads this single file instead of running many CLI
queries. Deterministic data plumbing, no LLM.

Usage:
    uv run python scripts/critique_prep.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

from _common import load_scenarios, mp, read_json, run_dir, write_json

# Company-profile fields worth giving the critic for grounding (subset of
# CompanyProfile); kept small so the critique input stays compact.
PROFILE_FIELDS = ("summary", "products", "target_customers")


def _company_context(domain: str | None) -> dict | None:
    """Return {domain, name, profile fields} for the grounding company."""
    if not domain:
        return None
    context: dict[str, object] = {"domain": domain}
    data = mp(["company", "view", domain], check=False)
    company = data.get("company") if isinstance(data, dict) else None
    if isinstance(company, dict):
        context["name"] = company.get("name")
        profile = company.get("profile") or {}
        if isinstance(profile, dict):
            for field in PROFILE_FIELDS:
                context[field] = profile.get(field)
    return context


def _workflow_wording(workflow_file: str) -> dict[str, object]:
    """Return the workflow wording under test: name, goal, instructions."""
    path = Path(workflow_file)
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError:
        return {}
    return {
        "name": data.get("name"),
        "goal": data.get("goal"),
        "instructions": data.get("instructions"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    verify = read_json(directory / "verify.json")
    reply_bodies = {s["key"]: s["reply_body"] for s in load_scenarios()}
    verify_by_key = {r["scenario_key"]: r for r in verify["scenarios"]}

    grounding = manifest.get("grounding_contact") or {}
    company = _company_context(grounding.get("company_domain"))

    records = []
    for scenario in load_scenarios():
        key = scenario["key"]
        result = verify_by_key.get(key, {})
        observed = result.get("observed", {})
        records.append(
            {
                "scenario_key": key,
                "label": scenario["label"],
                "expected_branch": scenario["expected_branch"],
                "inbound_reply": reply_bodies.get(key),
                "agent_reply": observed.get("reply_excerpt") or "",
                "observed": {
                    "outcome": observed.get("outcome"),
                    "contact_disabled": observed.get("contact_disabled"),
                    "agent_replied": observed.get("agent_replied"),
                    "followup_task": observed.get("followup_task"),
                },
                "pass": result.get("pass"),
                "notes": result.get("notes", []),
            }
        )

    bundle = {
        "workflow": _workflow_wording(manifest["workflow_file"]),
        "company": company,
        "scenarios": records,
    }
    write_json(directory / "critique_input.json", bundle)
    print(json.dumps({"prepared": len(records)}, indent=2))
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
