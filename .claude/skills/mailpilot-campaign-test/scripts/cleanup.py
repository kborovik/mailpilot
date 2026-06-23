"""Tear down the per-run scaffolding's live side effects.

Two cleanups, both idempotent and best-effort:
  1. Re-park each alias-contact on the neutral test company, removing the
     real-company link the run added. This keeps the real company's
     contact_count untouched at rest, so lead-contacts discovery is not skewed
     by the test (§V.96).
  2. Stop the ephemeral per-run workflow so it no longer shows as active on the
     sender account. Workflows cannot be deleted (no hard-delete, §V.10), so a
     stopped ``[campaign-test ...]`` row remains; that is expected and harmless.

The persistent alias-contacts and the neutral test company are intentionally
left in place for reuse by the next run. Writes ``cleanup.json``.

Usage:
    uv run python scripts/cleanup.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json

from _common import NEUTRAL_COMPANY_DOMAIN, mp, read_json, run_dir, write_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scaffold = read_json(directory / "scaffold.json")

    reparked = []
    for entry in scaffold["entries"]:
        if entry["linked_company_domain"] == NEUTRAL_COMPANY_DOMAIN:
            continue
        mp(
            [
                "contact",
                "update",
                entry["alias"],
                "--company-domain",
                NEUTRAL_COMPANY_DOMAIN,
            ],
            check=False,
        )
        reparked.append(entry["alias"])

    stop = mp(["workflow", "stop", scaffold["ephemeral_workflow_id"]], check=False)
    workflow_stopped = bool(stop.get("ok"))

    result = {
        "reparked_alias_contacts": reparked,
        "ephemeral_workflow_id": scaffold["ephemeral_workflow_id"],
        "workflow_stopped": workflow_stopped,
    }
    write_json(directory / "cleanup.json", result)
    print(
        json.dumps(
            {"reparked": len(reparked), "workflow_stopped": workflow_stopped},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
