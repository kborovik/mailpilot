"""Smoke-test runtime helper: pick a Q/A pair, load its source doc, check decline shape.

Subcommands:
  pick   [--type inscope|outscope] [--id ID]
         Emit one Q/A pair as JSON on stdout. Random unless --id given.
         Default --type=inscope.

  source --id ID
         Load the pair's `source_file` from the demo Drive folder via
         DriveClient impersonating `inbound@lab5.ca` and print its
         Markdown content to stdout. Exit non-zero when the file is
         absent from the folder (KB-drift signal). Used by gate B4 so
         the operator can grade groundedness against the live source.

  check  --id ID --reply-text "..." | --reply-file PATH
         Outscope-only post-V31. Validate an out-of-scope decline reply
         against the pair's `forbidden_token_pairs` and `decline_signals`.
         Inscope grading is operator-judged in gate B4 (see SKILL.md);
         calling `check` with an inscope pair exits 2.
         Emits {"id":..., "pass": bool, "reasons": [...], "details": {...}}
         on stdout. Exit 0 = pass, 1 = fail, 2 = inscope rejected.

Pairs live in qa_pairs.json next to this script.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

PAIRS_PATH = Path(__file__).parent / "qa_pairs.json"

# §V.31: source loader impersonates the same subject the demo agent uses,
# so a Drive ACL drift surfaces here the same way it would in production.
DEMO_FOLDER_ID = "1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat"
DEMO_SUBJECT = "inbound@lab5.ca"


def load_pairs() -> list[dict]:
    return json.loads(PAIRS_PATH.read_text(encoding="utf-8"))


def pick(args: argparse.Namespace) -> int:
    pairs = load_pairs()
    if args.id:
        match = next((p for p in pairs if p["id"] == args.id), None)
        if not match:
            print(json.dumps({"error": "not_found", "id": args.id}), file=sys.stderr)
            return 1
        chosen = match
    else:
        candidates = [p for p in pairs if p["type"] == args.type]
        if not candidates:
            print(
                json.dumps({"error": "no_pairs_of_type", "type": args.type}),
                file=sys.stderr,
            )
            return 1
        chosen = random.choice(candidates)
    print(json.dumps(chosen, indent=2))
    return 0


def source(args: argparse.Namespace) -> int:
    pairs = load_pairs()
    pair = next((p for p in pairs if p["id"] == args.id), None)
    if not pair:
        print(json.dumps({"error": "not_found", "id": args.id}), file=sys.stderr)
        return 1
    source_file = pair.get("source_file", "")
    if not source_file:
        print(
            json.dumps({"error": "pair_missing_source_file", "id": args.id}),
            file=sys.stderr,
        )
        return 1
    # Lazy import: keeps `qa.py pick`/`check` cheap and avoids forcing
    # google-api-python-client on operators who only run the existing paths.
    from mailpilot.drive import DriveClient

    client = DriveClient(DEMO_SUBJECT)
    files = client.list_markdown(DEMO_FOLDER_ID)
    match = next((f for f in files if f["name"] == source_file), None)
    if not match:
        print(
            json.dumps(
                {
                    "error": "source_file_not_in_drive",
                    "id": args.id,
                    "source_file": source_file,
                    "folder_id": DEMO_FOLDER_ID,
                }
            ),
            file=sys.stderr,
        )
        return 1
    doc = client.read_markdown(match["file_id"])
    sys.stdout.write(doc.get("content", ""))
    return 0


_DIGIT_RUN_RE = re.compile(r"\d[\d,.]*")


def check_outscope(pair: dict, reply: str) -> tuple[bool, list[str], dict]:
    forbidden_pairs = pair.get("forbidden_token_pairs", [])
    decline_signals = pair.get("decline_signals", [])
    question = pair.get("question", "")
    # §V.25: digits the agent could only be echoing (not fabricating) — present in
    # the original question. A match whose digit-runs all appear in the question
    # is a quoted recap, not a fabrication.
    question_digit_runs = set(_DIGIT_RUN_RE.findall(question))
    reasons: list[str] = []
    fabrications: list[list[str]] = []
    for vendor, spec_re in forbidden_pairs:
        # Vendor word followed within 60 chars by something matching spec_re.
        pattern = re.compile(
            re.escape(vendor) + r".{0,60}?" + spec_re, re.IGNORECASE | re.DOTALL
        )
        for match in pattern.finditer(reply):
            # Extend the match forward through any continuation of the digit run:
            # the non-greedy outer regex stops at the first digit ("5" of "50,000"),
            # but to compare against the question we need the full run.
            end = match.end()
            tail = _DIGIT_RUN_RE.match(reply, end - 1)
            window_end = tail.end() if tail else end
            window_digit_runs = _DIGIT_RUN_RE.findall(reply[match.start() : window_end])
            if window_digit_runs and all(
                run in question_digit_runs for run in window_digit_runs
            ):
                continue
            fabrications.append([vendor, spec_re])
            break
    if fabrications:
        reasons.append(f"reply fabricates vendor specs: {fabrications}")
    declined = any(sig in reply.lower() for sig in decline_signals)
    if not declined:
        reasons.append(
            f"no decline-signal language found (expected one of: {decline_signals})"
        )
    ok = not reasons
    return (
        ok,
        reasons,
        {
            "fabrications_found": fabrications,
            "declined": declined,
        },
    )


def check(args: argparse.Namespace) -> int:
    pairs = load_pairs()
    pair = next((p for p in pairs if p["id"] == args.id), None)
    if not pair:
        print(json.dumps({"error": "not_found", "id": args.id}), file=sys.stderr)
        return 1
    if pair["type"] == "inscope":
        # §V.31: inscope grading moved to operator judgement (gate B4).
        # Substring-match against curated `expected_tokens` produced false
        # negatives on phrasing variation; a verdict from the operator with
        # the live source loaded is the new contract.
        print(
            json.dumps(
                {
                    "error": "inscope_grading_moved",
                    "id": args.id,
                    "message": (
                        "inscope grading is now operator-judged in B4; see SKILL.md"
                    ),
                }
            ),
            file=sys.stderr,
        )
        return 2
    if args.reply_file:
        reply = Path(args.reply_file).read_text(encoding="utf-8")
    elif args.reply_text is not None:
        reply = args.reply_text
    else:
        reply = sys.stdin.read()
    ok, reasons, details = check_outscope(pair, reply)
    print(
        json.dumps(
            {
                "id": pair["id"],
                "type": pair["type"],
                "question": pair["question"],
                "pass": ok,
                "reasons": reasons,
                "details": details,
            },
            indent=2,
        )
    )
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="qa.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pick = sub.add_parser("pick", help="emit a random Q/A pair as JSON")
    p_pick.add_argument("--type", choices=["inscope", "outscope"], default="inscope")
    p_pick.add_argument("--id", help="pin to a specific pair id")
    p_pick.set_defaults(func=pick)

    p_source = sub.add_parser(
        "source",
        help="load the pair's source_file from the demo Drive folder and print it",
    )
    p_source.add_argument("--id", required=True)
    p_source.set_defaults(func=source)

    p_check = sub.add_parser(
        "check",
        help="validate an out-of-scope decline reply (inscope grading moved to B4)",
    )
    p_check.add_argument("--id", required=True)
    grp = p_check.add_mutually_exclusive_group()
    grp.add_argument("--reply-text", help="reply body verbatim")
    grp.add_argument("--reply-file", help="path to a file containing the reply body")
    p_check.set_defaults(func=check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
