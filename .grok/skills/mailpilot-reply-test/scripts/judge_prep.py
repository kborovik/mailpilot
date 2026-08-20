"""Assemble the judge subagent's inputs for the NL-graded cases (out-scope + compare).

``score_replies.py`` defers out-scope and compare verdicts to a judge subagent
(§V.105): it emits advisory signals but no verdict. This script bundles, per such
case that received a reply, everything the judge reads -- the question, the reply
body, the case rubric, the advisory signals, and (for compare) the source
datasheets pulled from the Drive KB via the SAME delegated access path the agent
uses (``DriveClient(inbound_email)``, §V.34-35) -- into ``judge_<run>.json`` so
the judge spends no tokens fetching. Out-scope cases carry no datasheet: the
product is absent from the KB by definition.

A reply that never arrived is already recorded NO_REPLY by ``score_replies.py``
and is skipped here (no judge needed). Drive faults degrade gracefully: the
bundle still carries the reply + rubric + signals, with a ``datasheet_error``
note, so the judge can rule on grounding and citation even without the source.

Usage:
    uv run python scripts/judge_prep.py --run-id <id> --run A|B
"""

from __future__ import annotations

import argparse
import json
import sys

from _common import read_json, repo_root, run_dir, write_json

JUDGED_TYPES = {"outscope", "compare"}


def _fetch_datasheets(
    inbound_email: str, folder_id: str, names: list[str]
) -> list[dict]:
    """Read each named Markdown datasheet from the KB folder (agent's view)."""
    sys.path.insert(0, str(repo_root() / "src"))
    from mailpilot.drive import DriveClient

    client = DriveClient(inbound_email)
    by_name = {
        entry["name"]: entry["file_id"] for entry in client.list_markdown(folder_id)
    }
    sheets: list[dict] = []
    for name in names:
        file_id = by_name.get(name)
        if not file_id:
            sheets.append({"name": name, "content": "", "error": "not found in folder"})
            continue
        document = client.read_markdown(file_id)
        sheets.append({"name": name, "content": document.get("content", "")})
    return sheets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run", required=True, choices=["A", "B"])
    args = parser.parse_args()

    directory = run_dir(args.run_id)
    manifest = read_json(directory / "run_manifest.json")
    preflight = read_json(directory / "preflight.json")
    scoring = read_json(directory / f"scoring_{args.run}.json")
    replies = read_json(directory / f"replies_{args.run}.json")["replies"]

    folder_id = preflight["drive_folder_id"]
    inbound_email = preflight["inbound_email"]

    cases: list[dict] = []
    for case in manifest["cases"]:
        if case["run"] != args.run or case["type"] not in JUDGED_TYPES:
            continue
        case_id = case["case_id"]
        reply = replies.get(case_id, {"status": "no_reply"})
        if reply.get("status") != "replied":
            continue
        bundle: dict = {
            "case_id": case_id,
            "type": case["type"],
            "question": case.get("question", ""),
            "reply_body": reply["body"],
            "rubric": case["grading"],
            "signals": scoring["cases"].get(case_id, {}).get("detail", {}),
            "datasheets": [],
        }
        if case["type"] == "compare":
            names = case.get("source_files") or case["grading"].get("must_cite", [])
            try:
                bundle["datasheets"] = _fetch_datasheets(
                    inbound_email, folder_id, names
                )
            except Exception as exc:
                bundle["datasheet_error"] = str(exc)
        cases.append(bundle)

    result = {"run": args.run, "cases": cases}
    write_json(directory / f"judge_{args.run}.json", result)
    print(
        json.dumps(
            {"run": args.run, "to_judge": [c["case_id"] for c in cases]}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
