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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".grok"
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
    stamp_at = body.index("_stamp_mechanical(")
    assert clear_at < stamp_at < execute_at


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


def test_address_change_scenario_hard_stop_distinct_from_ooo() -> None:
    """§V.161 / §B.131: address_change is hard-stop; auto_reply OOO is noop.

    Catalog fixture drives live campaign-test: address-change must disable the
    old contact; OOO must leave contact enabled with no agent reply.
    """
    common = _load("_common")
    by_key = {s["key"]: s for s in common.load_scenarios()}
    assert "address_change" in by_key
    assert "auto_reply" in by_key

    address = by_key["address_change"]
    body = address["reply_body"].lower()
    assert "email address has changed" in body or "address has changed" in body
    assert "update your records" in body
    assert "@" in address["reply_body"]  # new email present for note referral
    assert address["expect"]["contact_disabled"] is True
    assert address["expect"]["outcome"] == "any"

    ooo = by_key["auto_reply"]
    assert ooo["expect"]["contact_disabled"] is False
    assert ooo["expect"]["agent_replied"] is False
    assert ooo["expect"]["outcome"] == "none"
    assert ooo.get("mechanical") is True
    assert ooo["expect"]["task_status"] == "completed"
    assert ooo["expect"]["last_touch"] == 1
    assert ooo["expect"]["resume_within_days"] == 21
    assert "{event_week_month_day}" in ooo["reply_body"]
    assert "{return_weekday_month_day}" in ooo["reply_body"]
    assert "week of" in ooo["reply_body"].lower()
    assert "fully back online" in ooo["reply_body"].lower()


def test_left_company_last_day_is_hard_stop_not_ooo_pause() -> None:
    """#251: past-tense last-day auto-reply is DNC, not a year-pause."""
    common = _load("_common")
    by_key = {s["key"]: s for s in common.load_scenarios()}
    left = by_key["left_company"]
    body = left["reply_body"].lower()
    assert "last day" in body
    assert "was" in body
    assert left.get("mechanical") is True
    assert left["expect"]["contact_disabled"] is True
    assert left["expect"]["outcome"] == "any"
    assert left["expect"]["enrollment_updated"] is True
    assert "@" not in left["reply_body"]


def test_dnc_scenarios_require_enrollment_updated_at() -> None:
    """#253: setting do_not_contact must bump enrollment.updated_at."""
    common = _load("_common")
    dnc_keys = {"opt_out", "left_company", "address_change", "wrong_person"}
    by_key = {s["key"]: s for s in common.load_scenarios()}
    for key in dnc_keys:
        assert by_key[key]["expect"].get("enrollment_updated") is True, key


def test_ooo_date_tokens_event_week_past_return_next_monday() -> None:
    """#250: event week is already past; return Monday is this month, not +1y."""
    common = _load("_common")
    now = datetime(2026, 8, 20, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    tokens = common.reply_date_tokens(now=now)
    assert tokens["event_week_month_day"] == "August 17th"
    assert tokens["return_weekday_month_day"] == "Monday, August 24th"
    ooo = next(s for s in common.load_scenarios() if s["key"] == "auto_reply")
    rendered = common.render_reply_body(ooo["reply_body"], now=now)
    assert "week of August 17th" in rendered
    assert "Monday, August 24th" in rendered
    assert "{" not in rendered
    # Thursday + 21d window excludes a 2027 year-pause.
    resume = datetime(2026, 8, 24, tzinfo=now.tzinfo)
    year_pause = datetime(2027, 8, 17, tzinfo=now.tzinfo)
    assert (resume - now) < timedelta(days=21)
    assert (year_pause - now) > timedelta(days=21)

    monday = datetime(2026, 8, 24, 9, 0, tzinfo=now.tzinfo)
    monday_tokens = common.reply_date_tokens(now=monday)
    assert monday_tokens["event_week_month_day"] == "August 17th"
    assert monday_tokens["return_weekday_month_day"] == "Monday, August 31st"
