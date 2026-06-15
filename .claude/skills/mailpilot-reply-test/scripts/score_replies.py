"""Grade captured replies against the QA rubric (deterministic, no LLM).

Per case type (see references/grading.md):
  in-scope  -> every expected token present (whitespace-normalized substring).
  out-scope -> declined (>=1 decline signal) AND no fabricated spec
               (no forbidden brand+number co-occurrence).
  compare   -> every source file cited, every target model id mentioned, and a
               GFM pipe table present (structural proxy; depth left to analysis).
  no reply  -> NO_REPLY.

Writes ``scoring_<run>.json`` with a ``failed`` flag (true if any FAIL/NO_REPLY).
Always exits 0 so orchestration chains and teardown still run; branch on the flag.

Usage:
    uv run python scripts/score_replies.py --run-id <id> --run A|B
"""

from __future__ import annotations

import argparse
import json
import re

from _common import read_json, run_dir, write_json

TABLE_SEPARATOR = re.compile(r"\|\s*:?-{3,}")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _grade_inscope(body: str, grading: dict) -> tuple[str, dict]:
    body_n = _norm(body)
    hits = {tok: (_norm(tok) in body_n) for tok in grading["expected_tokens"]}
    verdict = "PASS" if all(hits.values()) else "FAIL"
    return verdict, {
        "token_hits": hits,
        "missing": [t for t, ok in hits.items() if not ok],
    }


def _grade_outscope(body: str, grading: dict) -> tuple[str, dict]:
    body_l = body.lower()
    forbidden = [
        pair
        for pair in grading.get("forbidden_token_pairs", [])
        if pair[0].lower() in body_l and re.search(pair[1], body, re.IGNORECASE)
    ]
    declined = [s for s in grading.get("decline_signals", []) if s.lower() in body_l]
    verdict = "PASS" if not forbidden and declined else "FAIL"
    return verdict, {"fabrication_hits": forbidden, "decline_signals_found": declined}


def _grade_compare(body: str, grading: dict) -> tuple[str, dict]:
    body_l = body.lower()
    body_n = _norm(body)
    cited = {
        sf: (sf.lower() in body_l or sf.removesuffix(".md").lower() in body_l)
        for sf in grading.get("must_cite", [])
    }
    mentioned = {m: (_norm(m) in body_n) for m in grading.get("must_mention", [])}
    has_table = (
        bool(TABLE_SEPARATOR.search(body)) if grading.get("require_table") else True
    )
    verdict = (
        "PASS"
        if all(cited.values()) and all(mentioned.values()) and has_table
        else "FAIL"
    )
    return verdict, {"cited": cited, "mentioned": mentioned, "has_table": has_table}


GRADERS = {
    "inscope": _grade_inscope,
    "outscope": _grade_outscope,
    "compare": _grade_compare,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run", required=True, choices=["A", "B"])
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    replies = read_json(directory / f"replies_{args.run}.json")["replies"]

    graded: dict[str, dict] = {}
    for case in (c for c in manifest["cases"] if c["run"] == args.run):
        case_id = case["case_id"]
        reply = replies.get(case_id, {"status": "no_reply"})
        if reply.get("status") != "replied":
            graded[case_id] = {
                "type": case["type"],
                "verdict": "NO_REPLY",
                "detail": {},
            }
            continue
        verdict, detail = GRADERS[case["type"]](reply["body"], case["grading"])
        graded[case_id] = {"type": case["type"], "verdict": verdict, "detail": detail}

    counts = {"PASS": 0, "FAIL": 0, "NO_REPLY": 0}
    for entry in graded.values():
        counts[entry["verdict"]] += 1
    failed = counts["FAIL"] > 0 or counts["NO_REPLY"] > 0

    result = {"run": args.run, "cases": graded, "summary": counts, "failed": failed}
    write_json(directory / f"scoring_{args.run}.json", result)
    print(
        json.dumps(
            {
                "run": args.run,
                "summary": counts,
                "verdicts": {k: v["verdict"] for k, v in graded.items()},
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
