"""Per-touch campaign copy lookup and brace render (§V.194).

Pure helpers: no DB, no Gmail, no Agent. Presence of a ``TouchCopy`` row
for N is the outbound template/LLM switch.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence
from typing import Any

from mailpilot.exceptions import TouchCopyRenderError
from mailpilot.models import CompanyView, ContactView, TouchCopy, TouchMessage, Workflow

PLACEHOLDERS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "title",
        "email",
        "company_name",
        "company_domain",
    }
)


def copy_for_touch(workflow: Workflow, n: int) -> TouchCopy | None:
    """Return the ``touch_copy`` row for touch N, or None (LLM path).

    Args:
        workflow: Loaded workflow whose ``touch_copy`` catalog is the switch.
        n: 1-based touch number.

    Returns:
        Matching ``TouchCopy`` row, or ``None`` when N has no row.
    """
    for row in workflow.touch_copy:
        if row.n == n:
            return row
    return None


def render_touch_copy(
    row: TouchCopy,
    contact: ContactView,
    company: CompanyView | None,
) -> TouchMessage:
    """Render one copy row against already-loaded CRM views (§V.194, §V.135).

    Closed brace placeholders, stdlib format, no Jinja and no expressions.
    Unknown token, leftover brace, empty used value, or empty T1 subject
    raise ``TouchCopyRenderError`` so the caller fails the task with no send.

    Args:
        row: Catalog copy for this N.
        contact: Pre-loaded contact view.
        company: Pre-loaded company view, or None when the contact has none.

    Returns:
        Validated ``TouchMessage`` ready for ``_deliver_touch``.

    Raises:
        TouchCopyRenderError: Render is not sendable.
    """
    values = _placeholder_values(contact, company)
    subject = _format_copy(row.subject, values)
    body = _format_copy(row.body, values)
    if row.n == 1 and not subject.strip():
        raise TouchCopyRenderError("n=1 subject must be non-empty after render")
    return TouchMessage(subject=subject or None, body=body)


def _placeholder_values(
    contact: ContactView, company: CompanyView | None
) -> dict[str, str | None]:
    """Map closed placeholder names to CRM values (None means empty)."""
    first = _optional_text(contact.first_name)
    last = _optional_text(contact.last_name)
    full = " ".join(part for part in (first, last) if part) or None
    return {
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "title": _optional_text(contact.title),
        "email": _optional_text(contact.email),
        "company_name": _optional_text(company.name if company is not None else None),
        "company_domain": _optional_text(
            company.domain if company is not None else None
        ),
    }


def _optional_text(value: str | None) -> str | None:
    """Strip; empty/None stays None so used placeholders fail closed."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _format_copy(template: str, values: dict[str, str | None]) -> str:
    """Format one subject or body string against the closed placeholder set."""
    try:
        return _ClosedFormatter().vformat(template, (), values)
    except TouchCopyRenderError:
        raise
    except (ValueError, KeyError) as exc:
        raise TouchCopyRenderError(f"leftover brace: {exc}") from exc


class _ClosedFormatter(string.Formatter):
    """stdlib Formatter that admits only closed placeholder identifiers."""

    def get_field(
        self,
        field_name: str,
        args: Sequence[Any],
        kwargs: Mapping[str, Any],
    ) -> tuple[str, str]:
        if (
            not field_name.isidentifier()
            or field_name not in PLACEHOLDERS
            or "." in field_name
            or "[" in field_name
        ):
            raise TouchCopyRenderError(f"unknown token {{{field_name}}}")
        value = kwargs.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise TouchCopyRenderError(f"empty placeholder {field_name}")
        return value, field_name

    def convert_field(self, value: object, conversion: str | None) -> object:
        if conversion is not None:
            raise TouchCopyRenderError(f"unknown token conversion {conversion}")
        return value

    def format_field(self, value: object, format_spec: str) -> str:
        if format_spec:
            raise TouchCopyRenderError(f"unknown token format {format_spec}")
        return super().format_field(value, format_spec)
