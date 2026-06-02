"""Smoke-test runtime helper: pick a Q/A pair, load its source doc(s), check decline shape.

Subcommands:
  pick   [--type inscope|outscope|compare] [--id ID]
         Emit one Q/A pair as JSON on stdout. Random unless --id given.
         Default --type=inscope.

  source --id ID
         Load the pair's source markdown from the demo Drive folder via
         DriveClient impersonating `inbound@lab5.ca` and print to stdout.
         - inscope: primary `source_file` -> prints that file's content.
           When the primary is absent from Drive AND `source_file_alts`
           is populated (§V.57(+) / §T.66), fall back to the first alt
           present in Drive so cross-source identifier collisions (e.g.
           model WS36-600-2 lives in two datasheets with divergent specs
           per §B.40) still render a viable grading source.
         - compare: `source_files` (list) -> prints each file's content
           preceded by a `=== SOURCE: <name> ===` separator so the
           operator can grade the agent's reply against every source.
         Exit non-zero when NO resolvable source file is present in the
         folder (KB-drift signal). Outscope pairs have no source -> exit 1.

  check  --id ID --reply-text "..." | --reply-file PATH
         Outscope-only post-§V.57. Validate an out-of-scope decline reply
         against the pair's `forbidden_token_pairs` and `decline_signals`.
         Inscope and compare grading is operator-judged (gates B4 / B7,
         see SKILL.md); calling `check` with a non-outscope pair exits 2.
         Emits {"id":..., "pass": bool, "reasons": [...], "details": {...}}
         on stdout. Exit 0 = pass, 1 = fail, 2 = non-outscope rejected.

Pair schema (qa_pairs.json):
  - inscope: type="inscope", source_file: str,
             source_file_alts: list[str] (optional, default [] -- carries
             cross-source identifier-collision alternates per §V.57(+))
  - compare: type="compare", source_files: list[str]  (>=2 files)
  - outscope: type="outscope", source_file: null
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

PAIRS_PATH = Path(__file__).parent / "qa_pairs.json"

# §V.57: source loader impersonates the same subject the demo agent uses,
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


def _resolve_source_files(pair: dict) -> list[str]:
    """Return the ordered candidate filenames a pair points at.

    inscope -> [source_file, *source_file_alts] (primary first; alts let
    `source` fall back when the primary is absent from Drive per §V.57(+));
    compare -> source_files (all required); outscope -> [].
    """
    if pair.get("type") == "compare":
        return list(pair.get("source_files") or [])
    primary = pair.get("source_file")
    candidates: list[str] = [primary] if primary else []
    for alt in pair.get("source_file_alts") or []:
        if alt and alt not in candidates:
            candidates.append(alt)
    return candidates


def source(args: argparse.Namespace) -> int:
    pairs = load_pairs()
    pair = next((p for p in pairs if p["id"] == args.id), None)
    if not pair:
        print(json.dumps({"error": "not_found", "id": args.id}), file=sys.stderr)
        return 1
    file_names = _resolve_source_files(pair)
    if not file_names:
        print(
            json.dumps(
                {
                    "error": "pair_has_no_source",
                    "id": args.id,
                    "type": pair.get("type"),
                }
            ),
            file=sys.stderr,
        )
        return 1
    # Lazy import: keeps `qa.py pick`/`check` cheap and avoids forcing
    # google-api-python-client on operators who only run the existing paths.
    from mailpilot.drive import DriveClient

    client = DriveClient(DEMO_SUBJECT)
    files = client.list_markdown(DEMO_FOLDER_ID)
    by_name = {f["name"]: f for f in files}
    # compare pairs require every listed source; inscope accepts the primary
    # or (per §V.57(+)) the first alt present in Drive so cross-source
    # identifier-collision pairs (§B.40) survive when one of the colliders
    # rotates out of the KB.
    if pair.get("type") == "compare":
        missing = [n for n in file_names if n not in by_name]
        if missing:
            print(
                json.dumps(
                    {
                        "error": "source_files_not_in_drive",
                        "id": args.id,
                        "missing": missing,
                        "folder_id": DEMO_FOLDER_ID,
                    }
                ),
                file=sys.stderr,
            )
            return 1
        resolved = file_names
    else:
        resolved = [n for n in file_names if n in by_name]
        if not resolved:
            print(
                json.dumps(
                    {
                        "error": "source_files_not_in_drive",
                        "id": args.id,
                        "missing": file_names,
                        "folder_id": DEMO_FOLDER_ID,
                    }
                ),
                file=sys.stderr,
            )
            return 1
        resolved = resolved[:1]
    multi = len(resolved) > 1
    for name in resolved:
        doc = client.read_markdown(by_name[name]["file_id"])
        content = doc.get("content", "")
        if multi:
            sys.stdout.write(f"=== SOURCE: {name} ===\n")
        sys.stdout.write(content)
        if multi:
            if not content.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.write("\n")
    return 0


_DIGIT_RUN_RE = re.compile(r"\d[\d,.]*")


def check_outscope(pair: dict, reply: str) -> tuple[bool, list[str], dict]:
    forbidden_pairs = pair.get("forbidden_token_pairs", [])
    decline_signals = pair.get("decline_signals", [])
    question = pair.get("question", "")
    # §V.56: digits the agent could only be echoing (not fabricating) — present in
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
    if pair["type"] != "outscope":
        # §V.57: inscope grading moved to operator judgement (gate B4 for
        # inscope, gate B7 for compare). Substring-match against curated
        # `expected_tokens` produced false negatives on phrasing variation;
        # a verdict from the operator with the live source(s) loaded is the
        # new contract. `check` stays as a verifier for outscope decline
        # shape only (regex against forbidden vendor/spec pairs).
        gate = "B7" if pair["type"] == "compare" else "B4"
        print(
            json.dumps(
                {
                    "error": "non_outscope_grading_moved",
                    "id": args.id,
                    "type": pair["type"],
                    "message": (
                        f"{pair['type']} grading is now operator-judged in gate "
                        f"{gate}; see SKILL.md"
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
    p_pick.add_argument(
        "--type",
        choices=["inscope", "outscope", "compare"],
        default="inscope",
    )
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
        help=(
            "validate an out-of-scope decline reply "
            "(inscope grading moved to B4; compare moved to B7)"
        ),
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
