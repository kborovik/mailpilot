"""Tests for the email_ops policy layer."""

from __future__ import annotations

from mailpilot.email_ops import (
    ContactDisabledError,
    ContactMissingError,
    CooldownError,
    EmailOpsError,
    OriginalMissingContactError,
    OriginalMissingThreadError,
    OriginalNotFoundError,
)


def test_exception_codes_match_agent_tool_strings() -> None:
    """`code` attributes must match the strings the agent tool returned
    historically, so the LLM-facing error contract is preserved."""
    assert ContactDisabledError.code == "contact_disabled"
    assert CooldownError.code == "cooldown"
    assert OriginalNotFoundError.code == "not_found"
    assert OriginalMissingThreadError.code == "no_thread"
    assert OriginalMissingContactError.code == "no_contact"
    assert ContactMissingError.code == "not_found"


def test_exceptions_inherit_from_email_ops_error() -> None:
    for cls in (
        ContactDisabledError,
        CooldownError,
        OriginalNotFoundError,
        OriginalMissingThreadError,
        OriginalMissingContactError,
        ContactMissingError,
    ):
        assert issubclass(cls, EmailOpsError)


def test_exception_str_carries_message() -> None:
    exc = ContactDisabledError("contact is bounced: hard fail")
    assert str(exc) == "contact is bounced: hard fail"
    assert exc.code == "contact_disabled"
