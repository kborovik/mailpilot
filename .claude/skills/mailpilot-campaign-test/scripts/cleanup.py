"""Tear down the per-run scaffolding's live side effects.

Three cleanups, all idempotent and best-effort:
  1. Re-enable the prospect contact if a branch (opt-out / wrong-person) left it
     disabled, and clear its notes, so the next run starts from a clean, enabled,
     note-free contact (§V.14). A do-not-contact / wrong-person branch appends
     notes that would otherwise poison the next run's read_contact grounding. The
     operator owns consent; this block is test scaffolding, not a real
     unsubscribe.
  2. Re-park the prospect contact on the neutral test company, removing the real
     grounding-company link the run added, so the real company's contact_count is
     untouched at rest and lead-contacts discovery is not skewed (§V.96).
  3. Stop every per-scenario ephemeral workflow so none shows as active on the
     sender account. Workflows cannot be deleted (no hard-delete, §V.10), so the
     stopped ``[campaign-test ...]`` rows remain; that is expected and harmless.

The persistent prospect contact and the neutral test company are intentionally
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
    prospect_email = scaffold["prospect_email"]

    # 1. Re-enable the prospect contact if a branch disabled it, then clear its
    # notes so the persistent contact is left clean for the next run (§V.14).
    view = mp(["contact", "view", prospect_email], check=False)
    was_disabled = bool(
        view.get("ok") and view.get("contact", {}).get("disabled_reason")
    )
    if was_disabled:
        mp(["contact", "enable", prospect_email], check=False)
    cleared = mp(["note", "remove", "--contact-email", prospect_email], check=False)
    notes_cleared = (
        cleared.get("note", {}).get("cleared", 0) if cleared.get("ok") else 0
    )

    # 2. Re-park the prospect contact on the neutral company.
    reparked = False
    if scaffold.get("linked_company_domain") != NEUTRAL_COMPANY_DOMAIN:
        mp(
            [
                "contact",
                "update",
                prospect_email,
                "--company-domain",
                NEUTRAL_COMPANY_DOMAIN,
            ],
            check=False,
        )
        reparked = True

    # 3. Stop every per-scenario ephemeral workflow.
    stopped = []
    for entry in scaffold["entries"]:
        result = mp(["workflow", "stop", entry["ephemeral_workflow_id"]], check=False)
        if result.get("ok"):
            stopped.append(entry["ephemeral_workflow_id"])

    summary = {
        "prospect_email": prospect_email,
        "contact_re_enabled": was_disabled,
        "notes_cleared": notes_cleared,
        "contact_reparked": reparked,
        "workflows_stopped": stopped,
    }
    write_json(directory / "cleanup.json", summary)
    print(
        json.dumps(
            {
                "contact_re_enabled": was_disabled,
                "notes_cleared": notes_cleared,
                "contact_reparked": reparked,
                "workflows_stopped": len(stopped),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
