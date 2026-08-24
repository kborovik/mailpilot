"""Routing for prompt-audit GitHub issues: code → this repo, toml → lab5.ca."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".grok"
    / "skills"
    / "mailpilot-prompt-audit"
    / "scripts"
)
sys.path.insert(0, str(_SCRIPTS))

from prepare_issues import (  # noqa: E402
    LAB5_GITHUB_REPO,
    classify_target,
    issue_body,
    issue_title,
)


def test_code_templates_routes_to_cwd() -> None:
    routing = classify_target("code:templates.py:_MUST_SEND")
    assert routing is not None
    assert routing["repo"] is None
    assert routing["path"] == "src/mailpilot/agent/templates.py"
    assert routing["field"] == "_MUST_SEND"


def test_code_classify_routes_to_cwd() -> None:
    routing = classify_target("code:classify.py")
    assert routing is not None
    assert routing["repo"] is None
    assert routing["path"] == "src/mailpilot/agent/classify.py"


def test_toml_routes_to_lab5() -> None:
    routing = classify_target("toml:acu-isv-leadership instructions")
    assert routing is not None
    assert routing["repo"] == LAB5_GITHUB_REPO
    assert (
        routing["path"]
        == "campaigns/acu-isv-leadership/workflows/acu-isv-leadership.toml"
    )
    assert routing["field"] == "instructions"


def test_toml_goal_routes_to_lab5() -> None:
    routing = classify_target("toml:var-sales-coclose goal")
    assert routing is not None
    assert routing["repo"] == LAB5_GITHUB_REPO
    assert routing["field"] == "goal"


def test_unknown_target_is_skipped() -> None:
    assert classify_target("mystery:foo") is None
    assert classify_target("toml:broken") is None


def test_title_includes_target_for_dedup() -> None:
    title = issue_title("code:classify.py", "Sharpen the routing cue")
    assert title.startswith("Prompt audit (code:classify.py):")
    assert "Sharpen the routing cue" in title


def test_body_names_acceptance_file() -> None:
    body = issue_body(
        {
            "target": "toml:acu-isv-leadership instructions",
            "summary": "Drop the duplicate send rule",
            "current": "Always send.",
            "proposed": "Send once.",
            "evidence": "tool-error rate 0.4",
            "confidence": "high",
            "priority": 1,
        },
        {
            "repo": LAB5_GITHUB_REPO,
            "path": "campaigns/acu-isv-leadership/workflows/acu-isv-leadership.toml",
            "field": "instructions",
        },
        "test-run",
        0,
    )
    assert body.startswith("Drop the duplicate send rule")
    assert "Prompt-audit target: `toml:acu-isv-leadership instructions`" in body
    assert "## Acceptance" in body
    assert "Re-import the workflow into Mailpilot" in body
    assert "`make check` passes" not in body


def test_mailpilot_body_asks_for_make_check() -> None:
    body = issue_body(
        {
            "target": "code:classify.py",
            "summary": "Sharpen the routing cue",
            "current": "Match loosely.",
            "proposed": "Match the goal only.",
            "evidence": "no_match 12",
            "confidence": "medium",
            "priority": 2,
        },
        {
            "repo": None,
            "path": "src/mailpilot/agent/classify.py",
            "field": "_INSTRUCTIONS",
        },
        "test-run",
        1,
    )
    assert "`make check` passes" in body
    assert "Re-import the workflow" not in body
