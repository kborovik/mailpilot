"""Smoke-test runtime helper: pick a Q/A pair, check the agent's reply.

Subcommands:
  pick   [--type inscope|outscope] [--id ID]
         Emit one Q/A pair as JSON on stdout. Random unless --id given.
         Default --type=inscope.

  check  --id ID --reply-text "..." | --reply-file PATH
         Validate the agent's reply against the pair's expectations.
         Emits {"id":..., "pass": bool, "reasons": [...], "details": {...}}
         on stdout. Exit 0 = pass, 1 = fail.

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


def check_inscope(pair: dict, reply: str) -> tuple[bool, list[str], dict]:
    expected = pair.get("expected_tokens", [])
    missing = [t for t in expected if t not in reply]
    src = pair.get("source_file", "")
    src_stem = Path(src).stem if src else ""
    src_cited = bool(src_stem) and (src in reply or src_stem in reply)
    reasons: list[str] = []
    if missing:
        reasons.append(f"missing expected tokens: {missing}")
    if src_stem and not src_cited:
        reasons.append(f"source file not cited: {src!r} (or stem {src_stem!r})")
    ok = not reasons
    return (
        ok,
        reasons,
        {
            "expected_tokens": expected,
            "missing_tokens": missing,
            "source_cited": src_cited,
        },
    )


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
    if args.reply_file:
        reply = Path(args.reply_file).read_text(encoding="utf-8")
    elif args.reply_text is not None:
        reply = args.reply_text
    else:
        reply = sys.stdin.read()
    if pair["type"] == "inscope":
        ok, reasons, details = check_inscope(pair, reply)
    else:
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

    p_check = sub.add_parser("check", help="validate an agent reply against a pair")
    p_check.add_argument("--id", required=True)
    grp = p_check.add_mutually_exclusive_group()
    grp.add_argument("--reply-text", help="reply body verbatim")
    grp.add_argument("--reply-file", help="path to a file containing the reply body")
    p_check.set_defaults(func=check)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
