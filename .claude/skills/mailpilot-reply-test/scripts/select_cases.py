"""Select the Run-A and Run-B test cases and write the run manifest.

Run-A = 1 in-scope case (all tokens specific, for a clean deterministic smoke).
Run-B = 2 in-scope + 1 compare + 1 out-of-scope, sent simultaneously. Selection
is seeded by ``run_id`` so a given run is reproducible while varying run-to-run.

Each case carries its grading payload (see references/grading.md) and a tagged
subject the agent's reply will preserve so we can correlate reply -> case.

Usage:
    uv run python scripts/select_cases.py --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import random
import re
from datetime import datetime

from _common import make_subject, qa_pairs_path, read_json, run_dir, write_json

ID_LIKE = re.compile(r"\d")


def _is_id_like(token: str) -> bool:
    """A clean model identifier: contains a digit, no spaces or colons."""
    return bool(ID_LIKE.search(token)) and not re.search(r"[\s:]", token)


def _strong(pair: dict) -> bool:
    """In-scope case with >=2 reasonably specific expected tokens."""
    tokens = pair.get("expected_tokens", [])
    return sum(1 for t in tokens if len(t) >= 5) >= 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    run_id = args.run_id

    directory = run_dir(run_id)
    preflight = read_json(directory / "preflight.json")
    pairs = read_json(qa_pairs_path())
    rng = random.Random(run_id)

    inscope = [p for p in pairs if p["type"] == "inscope"]
    compare = [p for p in pairs if p["type"] == "compare"]
    outscope = [p for p in pairs if p["type"] == "outscope"]

    # Map each in-scope source file -> its primary expected token, for deriving
    # compare "must mention" identifiers.
    primary_token = {
        p["source_file"]: p["expected_tokens"][0]
        for p in inscope
        if p.get("source_file") and p.get("expected_tokens")
    }

    # Run-A: in-scope where every token is specific (len >= 4) -> no false pass
    # from bare short numbers.
    run_a_pool = [
        p
        for p in inscope
        if _strong(p) and all(len(t) >= 4 for t in p["expected_tokens"])
    ]
    run_a_pool = run_a_pool or [p for p in inscope if _strong(p)]
    case_a = rng.choice(run_a_pool)

    # Run-B in-scope: 2 strong cases distinct from A.
    run_b_inscope_pool = [p for p in inscope if _strong(p) and p["id"] != case_a["id"]]
    run_b_inscope = rng.sample(run_b_inscope_pool, 2)

    # Run-B compare: every target maps to an id-like in-scope token so
    # "must mention" grading is reliable.
    eligible_compare = [
        p
        for p in compare
        if all(
            sf in primary_token and _is_id_like(primary_token[sf])
            for sf in p["source_files"]
        )
    ]
    case_cmp = rng.choice(eligible_compare or compare)

    case_out = rng.choice(outscope)

    def inscope_case(pair: dict, run: str) -> dict:
        return {
            "run": run,
            "case_id": pair["id"],
            "type": "inscope",
            "question": pair["question"],
            "source_file": pair.get("source_file"),
            "subject": make_subject(run_id, pair["id"], pair["expected_tokens"][0]),
            "grading": {"expected_tokens": pair["expected_tokens"]},
        }

    def compare_case(pair: dict, run: str) -> dict:
        must_mention = [
            primary_token[sf] for sf in pair["source_files"] if sf in primary_token
        ]
        hint = " vs ".join(must_mention) if must_mention else "compare"
        return {
            "run": run,
            "case_id": pair["id"],
            "type": "compare",
            "question": pair["question"],
            "source_files": pair["source_files"],
            "subject": make_subject(run_id, pair["id"], hint),
            "grading": {
                "must_cite": pair["source_files"],
                "must_mention": must_mention,
                "require_table": True,
            },
        }

    def outscope_case(pair: dict, run: str) -> dict:
        brand = (
            pair["forbidden_token_pairs"][0][0]
            if pair.get("forbidden_token_pairs")
            else "unknown"
        )
        return {
            "run": run,
            "case_id": pair["id"],
            "type": "outscope",
            "question": pair["question"],
            "source_file": None,
            "subject": make_subject(run_id, pair["id"], f"{brand} (not in KB)"),
            "grading": {
                "forbidden_token_pairs": pair.get("forbidden_token_pairs", []),
                "decline_signals": pair.get("decline_signals", []),
            },
        }

    cases = [
        inscope_case(case_a, "A"),
        inscope_case(run_b_inscope[0], "B"),
        inscope_case(run_b_inscope[1], "B"),
        compare_case(case_cmp, "B"),
        outscope_case(case_out, "B"),
    ]

    manifest = {
        "run_id": run_id,
        "created_at": datetime.now().astimezone().isoformat(),
        "outbound_account_id": preflight["outbound_account_id"],
        "inbound_account_id": preflight["inbound_account_id"],
        "outbound_email": preflight["outbound_email"],
        "inbound_email": preflight["inbound_email"],
        "workflow_id": preflight.get("workflow_id"),
        "drive_folder_id": preflight["drive_folder_id"],
        "cases": cases,
    }
    write_json(directory / "run_manifest.json", manifest)

    print(
        json.dumps(
            {
                "run_id": run_id,
                "run_a": [c["case_id"] for c in cases if c["run"] == "A"],
                "run_b": [
                    f"{c['case_id']}({c['type']})" for c in cases if c["run"] == "B"
                ],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
