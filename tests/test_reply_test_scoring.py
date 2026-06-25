"""Grading-logic guards for the mailpilot-reply-test scoring scripts (§V.105, §B.88).

The skill scripts live under ``.claude/skills/`` (outside the package roots) and
import a sibling ``_common`` module, so they are loaded by file path here rather
than imported as a package.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "mailpilot-reply-test"
    / "scripts"
)

_QA_PAIRS = _SCRIPTS.parent / "assets" / "QA-Pairs.json"

# An in-scope expected_token must be ATOMIC: a single contiguous value the reply
# cannot restructure away. The guard is an allowlist of atomic shapes, not a
# denylist of one brittle shape (§V.105, §B.109). The prior guard matched only
# the ``Label (Qualifier)`` header (§B.102), so a non-header non-atomic token
# slipped through -- a verb-bearing sentence fragment
# (``KDF 55 medium can remove over 99%``) or a multi-word product description
# (``(40) 8" TFC spiral wound membranes``). A grounded reply may reorder or
# re-split those, so the contiguous-substring match in ``_grade_inscope``
# false-FAILs, breaking the false-PASS-at-worst contract.

# A trailing parenthesized qualifier (``Label (Qualifier)``) renders across two
# table cells -- the §B.102 offender.
_TRAILING_QUALIFIER = re.compile(r"\S\s*\([^)]*\)\s*$")

# An atomic token carries at most this many whitespace pieces (a value plus a
# short unit and a short trailing qualifier, e.g. ``15 ppm RO Feed``); five or
# more pieces is a sentence fragment or product description.
_MAX_ATOMIC_PIECES = 4

# Verbs and auxiliaries that mark prose (a sentence fragment) rather than a value.
_SENTENCE_VERBS = frozenset(
    {
        "can",
        "cannot",
        "remove",
        "removes",
        "reduce",
        "reduces",
        "provide",
        "provides",
        "deliver",
        "delivers",
        "produce",
        "produces",
        "handle",
        "handles",
        "require",
        "requires",
        "is",
        "are",
        "was",
        "has",
        "have",
    }
)


def _is_atomic_inscope_token(token: str) -> bool:
    """True when ``token`` is an atomic in-scope value (§V.105 allowlist).

    Atomic = model id (``EDI-220``) | bare number (``28``) | number plus a short
    unit (``0.48 mm``), optionally trailing a short qualifier (``15 ppm RO
    Feed``) | a short label of at most two words (``Effective Size``). Anything
    else -- a ``Label (Qualifier)`` header, a verb-bearing sentence fragment, a
    multi-word product description -- is brittle.
    """
    normalized = re.sub(r"\s+", " ", token).strip()
    pieces = normalized.lower().split()
    if _TRAILING_QUALIFIER.search(normalized):
        return False
    if len(pieces) > _MAX_ATOMIC_PIECES:
        return False
    if any(piece in _SENTENCE_VERBS for piece in pieces):
        return False
    # A three-plus-word phrase with no numeric anchor is a multi-word
    # label/description; a one- or two-word label or any numeric value holds.
    has_digit = any(char.isdigit() for char in normalized)
    return has_digit or len(pieces) <= 2


def _is_brittle_inscope_token(token: str) -> bool:
    """True when ``token`` is NOT an atomic in-scope value (§V.105, §B.109).

    Inverse of the atomic-shape allowlist; the prior guard denylisted only the
    ``Label (Qualifier)`` header, so non-header non-atomic tokens slipped past.
    """
    return not _is_atomic_inscope_token(token)


def _load(module_name: str) -> Any:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        module_name, _SCRIPTS / f"{module_name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- in-scope: deterministic, unchanged (§V.105 in-scope leg) -----------------


def test_inscope_pass_when_every_token_present():
    score = _load("score_replies")
    grading = {"expected_tokens": ["0.48 mm", "260,000 GPD"]}
    body = "The membrane is 0.48 mm thick and rated at 260,000 GPD."
    verdict, detail = score.grade("inscope", body, grading)
    assert verdict == "PASS"
    assert detail["missing"] == []


def test_inscope_fail_lists_missing_token():
    score = _load("score_replies")
    grading = {"expected_tokens": ["0.48 mm", "260,000 GPD"]}
    body = "The membrane is 0.48 mm thick."
    verdict, detail = score.grade("inscope", body, grading)
    assert verdict == "FAIL"
    assert detail["missing"] == ["260,000 GPD"]


# --- out-scope: deferred to judge, advisory signals (§V.105, closes §B.88) -----


def test_outscope_defers_to_judge_with_signals():
    score = _load("score_replies")
    grading = {
        "forbidden_token_pairs": [["Pentair", r"\d"]],
        "decline_signals": ["do not carry"],
    }
    body = "We do not carry Pentair systems."
    verdict, detail = score.grade("outscope", body, grading)
    assert verdict == "JUDGE"
    assert detail["decline_signals_found"] == ["do not carry"]
    assert "fabrication_candidates" in detail


def test_outscope_brand_plus_digit_decline_is_not_a_verdict():
    # The §B.88 failure: a correct decline that echoes the asker's digits next to
    # the absent brand once fired a seed-unstable FAIL. The script no longer
    # decides out-scope, so a brand+digit co-occurrence is only an advisory
    # candidate for the judge -- never a FAIL emitted here.
    score = _load("score_replies")
    grading = {
        "forbidden_token_pairs": [["Pentair", r"\d"]],
        "decline_signals": ["do not carry"],
    }
    body = "We do not carry the Pentair line; you mentioned 300 GPM feedwater."
    verdict, detail = score.grade("outscope", body, grading)
    assert verdict == "JUDGE"
    assert detail["fabrication_candidates"] == [["Pentair", r"\d"]]


# --- compare: deferred to judge, advisory signals (§V.105) --------------------


def test_compare_defers_to_judge_with_token_hits_and_table():
    score = _load("score_replies")
    grading = {
        "must_cite": ["dow-eco-440i.md"],
        "must_mention": ["ECO-440i"],
        "require_table": True,
    }
    body = "Per dow-eco-440i, the ECO-440i:\n\n| Spec | Value |\n|---|---|\n| flow | 11500 gpd |"
    verdict, detail = score.grade("compare", body, grading)
    assert verdict == "JUDGE"
    assert detail["token_hits"] == {"dow-eco-440i.md": True, "ECO-440i": True}
    assert detail["has_table"] is True


# --- apply_judgments: judge verdict of record folded into scoring -------------


def test_apply_judgments_folds_verdict_and_recomputes_failed():
    apply_judgments = _load("apply_judgments")
    scoring = {
        "run": "B",
        "cases": {
            "qa-in-1": {"type": "inscope", "verdict": "PASS", "detail": {}},
            "qa-out-1": {
                "type": "outscope",
                "verdict": "JUDGE",
                "detail": {"fabrication_candidates": []},
            },
            "qa-cmp-1": {
                "type": "compare",
                "verdict": "JUDGE",
                "detail": {"has_table": True},
            },
        },
        "summary": {"PASS": 1, "FAIL": 0, "NO_REPLY": 0, "JUDGE": 2},
        "failed": False,
    }
    judgments = {
        "qa-out-1": {"verdict": "PASS", "rationale": "declined, no fabricated spec"},
        "qa-cmp-1": {"verdict": "FAIL", "rationale": "permeate flow misquoted"},
    }
    updated = apply_judgments.apply(scoring, judgments)
    assert updated["cases"]["qa-out-1"]["verdict"] == "PASS"
    assert updated["cases"]["qa-cmp-1"]["verdict"] == "FAIL"
    assert (
        updated["cases"]["qa-cmp-1"]["detail"]["rationale"] == "permeate flow misquoted"
    )
    assert updated["summary"] == {"PASS": 2, "FAIL": 1, "NO_REPLY": 0}
    assert updated["failed"] is True


def test_apply_judgments_unresolved_judge_stays_pending_and_fails():
    apply_judgments = _load("apply_judgments")
    scoring = {
        "run": "B",
        "cases": {
            "qa-out-1": {"type": "outscope", "verdict": "JUDGE", "detail": {}},
        },
        "summary": {"PASS": 0, "FAIL": 0, "NO_REPLY": 0, "JUDGE": 1},
        "failed": False,
    }
    updated = apply_judgments.apply(scoring, {})
    assert updated["summary"] == {"PASS": 0, "FAIL": 0, "NO_REPLY": 0}
    assert updated["failed"] is True


# --- atomic in-scope tokens guard (§V.105, closes §B.102, §B.109) -------------


def test_brittle_inscope_token_predicate():
    # Three brittle shapes, not just one (§B.109): the §B.102 header label plus a
    # parenthesized qualifier, a verb-bearing sentence fragment, and a multi-word
    # product description. The prior denylist caught only the first.
    assert _is_brittle_inscope_token("Maximum (15 ppm RO Feed)") is True
    assert _is_brittle_inscope_token("KDF 55 medium can remove over 99%") is True
    assert (
        _is_brittle_inscope_token("remove up to 98% of water-soluble cations") is True
    )
    assert _is_brittle_inscope_token('(40) 8" TFC spiral wound membranes') is True
    # Atomic values the reply cannot restructure away pass: a split-out fact, a
    # model id, a bare number, a number plus short unit, a number plus a short
    # trailing qualifier, a short label, and a count-prefixed value whose only
    # parenthesis leads (not a trailing qualifier).
    assert _is_brittle_inscope_token("over 99%") is False
    assert _is_brittle_inscope_token("98%") is False
    assert _is_brittle_inscope_token("40") is False
    assert _is_brittle_inscope_token("15 ppm RO Feed") is False
    assert _is_brittle_inscope_token("EDI-220") is False
    assert _is_brittle_inscope_token("28") is False
    assert _is_brittle_inscope_token("0.48 mm") is False
    assert _is_brittle_inscope_token("Effective Size") is False
    assert _is_brittle_inscope_token('(20) 8"x40"') is False


def test_inscope_expected_tokens_are_atomic():
    # Every in-scope rubric token must be atomic; a layout-dependent token
    # false-FAILs a grounded reply, breaking the false-PASS-at-worst contract.
    # The §B.109 offenders -- a verb-bearing sentence fragment and a multi-word
    # product description -- were atomized in QA-Pairs.json (qa-in-006,
    # qa-in-009); the guard now flags both shapes, so neither re-enters a rubric.
    assert _is_brittle_inscope_token("KDF 55 medium can remove over 99%") is True
    assert _is_brittle_inscope_token('(40) 8" TFC spiral wound membranes') is True
    pairs = json.loads(_QA_PAIRS.read_text())
    brittle = [
        (pair["id"], token)
        for pair in pairs
        if pair.get("type") == "inscope"
        for token in pair.get("expected_tokens", [])
        if _is_brittle_inscope_token(token)
    ]
    assert brittle == [], f"non-atomic in-scope expected_tokens: {brittle}"
