"""Policy layer for outbound email operations.

This module is the single source of truth for the rules that govern
sending and replying. Both ``cli.py`` and ``agent/tools.py`` call into
``send_email`` / ``reply_email`` here, so guards (contact-status,
cooldown, reply preconditions) and side effects (enrollment activation)
live in one place.

Failures raise typed ``EmailOpsError`` subclasses. Callers convert them
to their native error shapes -- a dict for the agent, ``output_error``
JSON for the CLI.
"""

from __future__ import annotations


class EmailOpsError(Exception):
    """Base class for email policy violations.

    Subclasses set a class-level ``code`` matching the legacy agent-tool
    error string, so the LLM-facing contract is unchanged.
    """

    code: str = "email_ops_error"


class ContactDisabledError(EmailOpsError):
    """Recipient contact is bounced or unsubscribed; send blocked."""

    code = "contact_disabled"


class CooldownError(EmailOpsError):
    """Prior unsolicited cold outbound is within the cooldown window."""

    code = "cooldown"


class OriginalNotFoundError(EmailOpsError):
    """Reply target email_id does not resolve to a row."""

    code = "not_found"


class OriginalMissingThreadError(EmailOpsError):
    """Reply target has no gmail_thread_id, so no thread to reply into."""

    code = "no_thread"


class OriginalMissingContactError(EmailOpsError):
    """Reply target has no contact_id, so no recipient can be derived."""

    code = "no_contact"


class ContactMissingError(EmailOpsError):
    """Reply target's contact_id no longer resolves to a contact row."""

    code = "not_found"
