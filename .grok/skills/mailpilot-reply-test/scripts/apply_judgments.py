"""Fold the judge subagent's verdicts into the scoring file (verdict of record).

For out-scope + compare cases ``score_replies.py`` writes a ``"JUDGE"`` sentinel
verdict (§V.105); the judge's PASS/FAIL is authoritative. This script reads the
judge's ``judgments_<run>.json`` (``{case_id: {"verdict": ..., "rationale": ...}}``),
replaces each sentinel with the judged verdict, records the rationale on the
case detail, and recomputes the ``summary`` counts and ``failed`` flag so the
report and failure-escalation phases read a single finalized source of truth.

A ``JUDGE`` case the judge left unresolved keeps the operator honest: it is
counted as pending and forces ``failed`` true rather than vanishing from the
pass rate.

Usage:
    uv run python scripts/apply_judgments.py --run-id <id> --run A|B
"""

from __future__ import annotations

import argparse
import json

from _common import read_json, run_dir, write_json


def apply(scoring: dict, judgments: dict) -> dict:
    """Resolve JUDGE sentinels with the judge's verdicts; recompute summary."""
    cases = scoring["cases"]
    for case_id, verdict_info in judgments.items():
        entry = cases.get(case_id)
        if entry is None or entry.get("verdict") != "JUDGE":
            continue
        entry["verdict"] = verdict_info["verdict"]
        entry.setdefault("detail", {})["rationale"] = verdict_info.get("rationale", "")

    counts = {"PASS": 0, "FAIL": 0, "NO_REPLY": 0}
    pending = 0
    for entry in cases.values():
        if entry["verdict"] in counts:
            counts[entry["verdict"]] += 1
        else:
            pending += 1
    scoring["summary"] = counts
    scoring["failed"] = counts["FAIL"] > 0 or counts["NO_REPLY"] > 0 or pending > 0
    return scoring


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run", required=True, choices=["A", "B"])
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    scoring = read_json(directory / f"scoring_{args.run}.json")
    judgments = read_json(directory / f"judgments_{args.run}.json")
    updated = apply(scoring, judgments)
    write_json(directory / f"scoring_{args.run}.json", updated)
    print(
        json.dumps(
            {
                "run": args.run,
                "summary": updated["summary"],
                "failed": updated["failed"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
