"""Routing for prompt-audit GitHub issues: code → this repo, toml → lab5.ca."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".grok"
    / "skills"
    / "mailpilot-prompt-audit"
    / "scripts"
)


def _load(name: str) -> types.ModuleType:
    saved_common = sys.modules.pop("_common", None)
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
        sys.modules.pop("_common", None)
        if saved_common is not None:
            sys.modules["_common"] = saved_common
    return module


@pytest.fixture(scope="module")
def prepare() -> types.ModuleType:
    return _load("prepare_issues")


def test_code_templates_routes_to_cwd(prepare: types.ModuleType) -> None:
    routing = prepare.classify_target("code:templates.py:_MUST_SEND")
    assert routing is not None
    assert routing["repo"] is None
    assert routing["path"] == "src/mailpilot/agent/templates.py"
    assert routing["field"] == "_MUST_SEND"


def test_code_classify_routes_to_cwd(prepare: types.ModuleType) -> None:
    routing = prepare.classify_target("code:classify.py")
    assert routing is not None
    assert routing["repo"] is None
    assert routing["path"] == "src/mailpilot/agent/classify.py"


def test_toml_routes_to_lab5(prepare: types.ModuleType) -> None:
    routing = prepare.classify_target("toml:acu-isv-leadership instructions")
    assert routing is not None
    assert routing["repo"] == prepare.LAB5_GITHUB_REPO
    assert (
        routing["path"]
        == "campaigns/acu-isv-leadership/workflows/acu-isv-leadership.toml"
    )
    assert routing["field"] == "instructions"


def test_toml_goal_routes_to_lab5(prepare: types.ModuleType) -> None:
    routing = prepare.classify_target("toml:var-sales-coclose goal")
    assert routing is not None
    assert routing["repo"] == prepare.LAB5_GITHUB_REPO
    assert routing["field"] == "goal"


def test_unknown_target_is_skipped(prepare: types.ModuleType) -> None:
    assert prepare.classify_target("mystery:foo") is None
    assert prepare.classify_target("toml:broken") is None


def test_title_includes_target_for_dedup(prepare: types.ModuleType) -> None:
    title = prepare.issue_title("code:classify.py", "Sharpen the routing cue")
    assert title.startswith("Prompt audit (code:classify.py):")
    assert "Sharpen the routing cue" in title


def test_body_names_acceptance_file(prepare: types.ModuleType) -> None:
    body = prepare.issue_body(
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
            "repo": prepare.LAB5_GITHUB_REPO,
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


def test_mailpilot_body_asks_for_make_check(prepare: types.ModuleType) -> None:
    body = prepare.issue_body(
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
