"""§V.194 TouchCopy lookup + brace render (pure)."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from mailpilot.agent.invoke import invoke_workflow_agent
from mailpilot.exceptions import TouchCopyRenderError
from mailpilot.models import CompanyView, ContactView, TouchCopy, Workflow
from mailpilot.touch_copy import copy_for_touch, render_touch_copy

_NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _workflow(*rows: TouchCopy) -> Workflow:
    return Workflow(
        id="w1",
        name="demo-outreach",
        template="outbound-general",
        type="outbound",
        account_id="a1",
        account_email="a@example.com",
        created_at=_NOW,
        updated_at=_NOW,
        touch_copy=list(rows),
    )


def _contact(**overrides: object) -> ContactView:
    fields: dict[str, object] = {
        "id": "c1",
        "email": "ada@acme.com",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "title": "VP Sales",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    fields.update(overrides)
    return ContactView(**fields)  # type: ignore[arg-type]


def _company(**overrides: object) -> CompanyView:
    fields: dict[str, object] = {
        "id": "co1",
        "name": "Acme",
        "domain": "acme.com",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    fields.update(overrides)
    return CompanyView(**fields)  # type: ignore[arg-type]


def test_copy_for_touch_returns_row_for_n() -> None:
    """§V.194: copy_for_touch is the per-N dispatch key."""
    t1 = TouchCopy(n=1, subject="Hi {first_name}", body="Hello")
    t2 = TouchCopy(n=2, subject="", body="Follow up")
    workflow = _workflow(t1, t2)
    assert copy_for_touch(workflow, 1) == t1
    assert copy_for_touch(workflow, 2) == t2
    assert copy_for_touch(workflow, 3) is None


def test_copy_for_touch_empty_list_is_all_llm() -> None:
    """§V.194: empty touch_copy means every N is compose-only LLM."""
    assert copy_for_touch(_workflow(), 1) is None


def test_render_touch_copy_substitutes_closed_placeholders() -> None:
    """§V.194: brace stdlib format over the closed placeholder set."""
    row = TouchCopy(
        n=1,
        subject="Quick question, {first_name}",
        body="Hi {full_name} at {company_name} ({company_domain}). {title} {email}",
    )
    message = render_touch_copy(row, _contact(), _company())
    assert message.subject == "Quick question, Ada"
    assert message.body == ("Hi Ada Lovelace at Acme (acme.com). VP Sales ada@acme.com")


def test_render_touch_copy_unknown_token_fails() -> None:
    """§V.194: unknown `{token}` fails closed; no send."""
    row = TouchCopy(n=1, subject="Hi", body="Hello {nope}")
    with pytest.raises(TouchCopyRenderError, match="unknown"):
        render_touch_copy(row, _contact(), _company())


def test_render_touch_copy_leftover_brace_fails() -> None:
    """§V.194: unmatched leftover brace fails closed."""
    row = TouchCopy(n=1, subject="Hi", body="Hello {first_name")
    with pytest.raises(TouchCopyRenderError, match="brace"):
        render_touch_copy(row, _contact(), _company())


def test_render_touch_copy_empty_placeholder_fails() -> None:
    """§V.194: null/empty used placeholder fails; no empty-string substitute."""
    row = TouchCopy(n=1, subject="Hi {first_name}", body="Hello")
    with pytest.raises(TouchCopyRenderError, match="empty"):
        render_touch_copy(row, _contact(first_name=None), _company())


def test_render_touch_copy_empty_t1_subject_fails() -> None:
    """§V.194: n=1 subject empty after render fails."""
    row = TouchCopy(n=1, subject="   ", body="Hello")
    with pytest.raises(TouchCopyRenderError, match="subject"):
        render_touch_copy(row, _contact(), _company())


def test_render_touch_copy_n2_empty_subject_ok() -> None:
    """§V.194: n>=2 may leave subject empty (thread continue)."""
    row = TouchCopy(n=2, subject="", body="Following up, {first_name}.")
    message = render_touch_copy(row, _contact(), _company())
    assert message.subject is None
    assert message.body == "Following up, Ada."


def test_render_touch_copy_rejects_format_expressions() -> None:
    """§V.194: no Jinja, no expressions, no format specs."""
    row = TouchCopy(n=1, subject="Hi", body="{first_name:>10}")
    with pytest.raises(TouchCopyRenderError):
        render_touch_copy(row, _contact(), _company())


def test_copy_row_dispatch_precedes_build_model() -> None:
    """§V.194: copy-row gate sits above build_model in invoke_workflow_agent."""
    src = inspect.getsource(invoke_workflow_agent)
    assert src.index("copy_row = copy_for_touch") < src.index("model = build_model")
