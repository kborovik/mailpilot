"""Campaign-test handle_replies isolates shared-prospect state between scenarios.

Prior branches write conclude_enrollment notes on the shared prospect; those
must be cleared before the next scenario so pre-fed ContactView notes do not
poison branch judgment (§V.14 / §B.118 mid-run).
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".claude"
    / "skills"
    / "mailpilot-campaign-test"
    / "scripts"
)


def _load(name: str) -> types.ModuleType:
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
    return module


def test_handle_scenario_clears_prospect_notes_before_agent() -> None:
    """handle_replies clears notes on the shared prospect before execute_task."""
    handle = _load("handle_replies")
    # Body only -- the docstring also mentions these names.
    source = inspect.getsource(handle._handle_scenario)
    body = source.split('"""', 2)[-1] if '"""' in source else source
    assert "clear_contact_notes(PROSPECT_EMAIL)" in body
    # Ordering: enable, then clear notes, then connect/execute.
    enable_at = body.index("_ensure_enabled()")
    clear_at = body.index("clear_contact_notes(PROSPECT_EMAIL)")
    execute_at = body.index("execute_task(")
    assert enable_at < clear_at < execute_at


def test_question_scenario_reply_is_answerable_without_product_kb() -> None:
    """question reply must be answerable from calendar-link ready copy alone.

    A MailPilot product-pricing probe forces contact_later on Acumatica-only
    workflows (no product KB); a calendar/times clarify keeps the branch in
    scope for answer + re-offer.
    """
    common = _load("_common")
    scenarios = common.load_scenarios()
    question = next(s for s in scenarios if s["key"] == "question")
    body = question["reply_body"].lower()
    assert "calendar" in body or "times" in body
    assert "sourced reply" not in body
    assert "out of scope" not in body
