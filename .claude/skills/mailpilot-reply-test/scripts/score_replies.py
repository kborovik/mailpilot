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

# Fold the Unicode dash family to ASCII "-" so typographic variants (en/em dash,
# minus sign, non-breaking hyphen) match the ASCII hyphen used in spec tokens.
# Dash variants are typographic, never semantic, in spec values. Code points:
# U+2010 hyphen, U+2011 non-breaking hyphen, U+2012 figure dash, U+2013 en dash,
# U+2014 em dash, U+2015 horizontal bar, U+2212 minus sign.
_DASHES = str.maketrans(
    dict.fromkeys(
        map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)), "-"
    )
)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().translate(_DASHES)).strip()


def _loose(tok: str, body_n: str) -> bool:
    """Tolerate separator drift in number+unit tokens, e.g. "54-in" vs "54 in.".

    Only fires for tokens shaped ``<number><sep><short-unit>``; the numeric part
    must still match exactly, so a wrong dimension cannot pass.
    """
    match = re.fullmatch(r"(\d[\d.,]*)[-.\s]+([a-z]{1,4})\.?", _norm(tok))
    if not match:
        return False
    number, unit = match.group(1), match.group(2)
    return (
        re.search(rf"{re.escape(number)}[-.\s]*{re.escape(unit)}\b", body_n) is not None
    )


def _grade_inscope(body: str, grading: dict, question: str = "") -> tuple[str, dict]:
    body_n = _norm(body)
    hits = {
        tok: (_norm(tok) in body_n or _loose(tok, body_n))
        for tok in grading["expected_tokens"]
    }
    verdict = "PASS" if all(hits.values()) else "FAIL"
    return verdict, {
        "token_hits": hits,
        "missing": [t for t, ok in hits.items() if not ok],
    }


# Max characters allowed between a brand mention and a digit before the pair is
# treated as a fabricated spec rather than two incidental co-occurrences.
_FABRICATION_WINDOW = 40


def _near(brand: str, digit_pattern: str, surface: str) -> bool:
    """True if ``digit_pattern`` matches within the fabrication window of ``brand``.

    "Fabricated a spec for the absent product" means a number sits *next to* the
    brand name, not merely somewhere in the same reply. Bounding the distance
    stops an incidental digit (a leaked signature host, a stray reference number)
    from tripping a brand named only to decline.
    """
    brand_escaped = re.escape(brand)
    window = _FABRICATION_WINDOW
    pattern = (
        rf"{brand_escaped}.{{0,{window}}}(?:{digit_pattern})"
        rf"|(?:{digit_pattern}).{{0,{window}}}{brand_escaped}"
    )
    return re.search(pattern, surface, re.IGNORECASE | re.DOTALL) is not None


def _grade_outscope(body: str, grading: dict, question: str = "") -> tuple[str, dict]:
    body_l = body.lower()
    # A legitimate decline names the absent product and often restates the
    # asker's own figures or links a referral page, so a bare "brand + any digit"
    # check yields false positives. Mask digits the asker themselves supplied
    # (question echo) and URL hosts (any TLD, so the agent's own ``lab5.ca``
    # signature does not leak a digit) before testing the forbidden pattern, and
    # require the digit to sit *near* the brand, so only numbers the agent
    # *invented* for the absent product count as fabrication.
    echoed = set(re.findall(r"\d[\d.,]*", question))
    surface = re.sub(r"https?://\S+|\b\S+\.[a-z]{2,}\b", " ", body)
    for value in echoed:
        surface = surface.replace(value, " ")
    forbidden = [
        pair
        for pair in grading.get("forbidden_token_pairs", [])
        if pair[0].lower() in body_l and _near(pair[0], pair[1], surface)
    ]
    declined = [s for s in grading.get("decline_signals", []) if s.lower() in body_l]
    verdict = "PASS" if not forbidden and declined else "FAIL"
    return verdict, {"fabrication_hits": forbidden, "decline_signals_found": declined}


def _grade_compare(body: str, grading: dict, question: str = "") -> tuple[str, dict]:
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
        verdict, detail = GRADERS[case["type"]](
            reply["body"], case["grading"], case.get("question", "")
        )
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
