"""Grade captured replies against the QA rubric (see references/grading.md).

Per case type the verdict source differs (§V.105):
  in-scope  -> graded here, deterministically: every expected token present
               (whitespace-normalized substring); false-PASS at worst, never a
               false FAIL.
  out-scope -> NOT decided here. Emits advisory signals (fabrication_candidates,
               decline_signals_found) and a ``"JUDGE"`` sentinel verdict; a
               judge subagent is the verdict of record.
  compare   -> NOT decided here. Emits advisory signals (token_hits, has_table)
               and a ``"JUDGE"`` sentinel; the judge subagent decides.
  no reply  -> NO_REPLY (any type).

Deterministic substring/regex grading of free-form NL replies (polite decline,
cross-datasheet compare) yields false and seed-unstable verdicts (§B.88), so the
out-scope/compare legs only surface signals; the judge reads the reply body, the
rubric, those signals, and the source datasheet to rule (see SKILL.md step 3b).

Writes ``scoring_<run>.json`` with a ``failed`` flag (true if any FAIL/NO_REPLY;
``JUDGE`` cases stay pending until ``apply_judgments.py`` folds the verdict in).
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

# Sentinel verdict for cases whose verdict of record is the judge subagent's, not
# this script's. ``apply_judgments.py`` replaces it with the judged PASS/FAIL.
JUDGE = "JUDGE"

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

# Fold the multiplication-sign family to ASCII "x" so a size token rendered
# with a multiplication glyph matches an expected token stored as "8\"x40\"".
# Multiplication glyphs are typographic, never semantic, in spec dimension
# tokens. Code points: U+00D7 multiplication sign, U+2715 multiplication x,
# U+2716 heavy multiplication x, U+2217 asterisk operator.
_TIMES = str.maketrans(dict.fromkeys(map(chr, (0x00D7, 0x2715, 0x2716, 0x2217)), "x"))


def _norm(text: str) -> str:
    # Strip thousands-separator commas so a datasheet figure rendered as
    # "6,000 gpd" matches an expected token stored as "6000 gpd" (and the
    # reverse). The lookarounds delete a comma only when a digit sits on both
    # sides, so list commas, trailing commas, and decimals are preserved. The
    # dash and multiplication folds run first so a typographic glyph in a size
    # token matches the ASCII rubric form. Both the body and every token pass
    # through _norm, so every rule here is symmetric.
    folded = text.lower().translate(_DASHES).translate(_TIMES)
    collapsed = re.sub(r"\s+", " ", folded).strip()
    return re.sub(r"(?<=\d),(?=\d)", "", collapsed)


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


def _grade_inscope(body: str, grading: dict) -> tuple[str, dict]:
    """Deterministic in-scope grade: every expected token must be present.

    An optional ``token_aliases`` map lets a canonical token match on an
    accepted alternate surface form (e.g. ``EDI`` for ``Electrodeionization``)
    so a factually correct reply never false-FAILs on wording. ``token_hits``
    and ``missing`` stay keyed by the canonical token.
    """
    body_n = _norm(body)
    aliases = grading.get("token_aliases", {})

    def _hit(token: str) -> bool:
        forms = [token, *aliases.get(token, [])]
        return any(_norm(form) in body_n or _loose(form, body_n) for form in forms)

    hits = {tok: _hit(tok) for tok in grading["expected_tokens"]}
    verdict = "PASS" if all(hits.values()) else "FAIL"
    return verdict, {
        "token_hits": hits,
        "missing": [t for t, ok in hits.items() if not ok],
    }


def _signals_outscope(body: str, grading: dict) -> dict:
    """Advisory signals for an out-of-scope reply -- no verdict (§V.105).

    A legitimate decline names the absent product and routinely restates the
    asker's own figures or links a referral page, so a "brand + any digit" check
    fires false positives (§B.88). The signals only flag *candidates*: the brand
    string is present AND its forbidden pattern matches somewhere in the body.
    The judge reads the actual reply and the rubric to rule whether a candidate
    is a fabricated spec or an incidental echo.
    """
    body_l = body.lower()
    fabrication_candidates = [
        pair
        for pair in grading.get("forbidden_token_pairs", [])
        if pair[0].lower() in body_l
        and re.search(pair[1], body, re.IGNORECASE) is not None
    ]
    declined = [s for s in grading.get("decline_signals", []) if s.lower() in body_l]
    return {
        "fabrication_candidates": fabrication_candidates,
        "decline_signals_found": declined,
    }


def _signals_compare(body: str, grading: dict) -> dict:
    """Advisory signals for a compare reply -- no verdict (§V.105).

    Reports presence of each cited source file and mentioned model id under one
    ``token_hits`` map, plus whether a GFM pipe table is present. Whether the
    compared numbers are correct (the part a structural proxy cannot prove) is
    left to the judge, which reads the source datasheets.
    """
    body_l = body.lower()
    body_n = _norm(body)
    token_hits = {
        source: (
            source.lower() in body_l or source.removesuffix(".md").lower() in body_l
        )
        for source in grading.get("must_cite", [])
    }
    token_hits.update(
        {model: (_norm(model) in body_n) for model in grading.get("must_mention", [])}
    )
    has_table = bool(TABLE_SEPARATOR.search(body))
    return {"token_hits": token_hits, "has_table": has_table}


_SIGNALS = {"outscope": _signals_outscope, "compare": _signals_compare}


def grade(case_type: str, body: str, grading: dict) -> tuple[str, dict]:
    """Return (verdict, detail) for a replied case.

    in-scope is decided here; out-scope and compare get a ``"JUDGE"`` sentinel
    verdict plus advisory signals (§V.105) for the judge subagent to resolve.
    """
    if case_type == "inscope":
        return _grade_inscope(body, grading)
    return JUDGE, _SIGNALS[case_type](body, grading)


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
        verdict, detail = grade(case["type"], reply["body"], case["grading"])
        graded[case_id] = {"type": case["type"], "verdict": verdict, "detail": detail}

    counts = {"PASS": 0, "FAIL": 0, "NO_REPLY": 0, JUDGE: 0}
    for entry in graded.values():
        counts[entry["verdict"]] += 1
    # JUDGE cases are pending the judge's verdict (apply_judgments.py finalizes);
    # they do not set failed here.
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
