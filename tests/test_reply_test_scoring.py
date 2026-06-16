"""Grading-logic guards for the mailpilot-reply-test scoring scripts (§V.105, §B.88).

The skill scripts live under ``.claude/skills/`` (outside the package roots) and
import a sibling ``_common`` module, so they are loaded by file path here rather
than imported as a package.
"""

from __future__ import annotations

import importlib.util
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
