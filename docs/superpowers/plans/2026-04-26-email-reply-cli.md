# `email reply` and `email_ops` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `mailpilot email reply` and unify CLI/agent send paths through a new `mailpilot.email_ops` policy module so both surfaces apply the same guards.

**Architecture:** Three layers -- callers (`cli.py`, `agent/tools.py`) -> policy (`email_ops.py`, new) -> transport (`sync.send_email`, unchanged). The policy module raises typed exceptions; each caller catches them and renders to its native error shape (LLM dict for the agent, `output_error` JSON for the CLI). Spec: `docs/superpowers/specs/2026-04-26-email-reply-cli-design.md`.

**Tech Stack:** Python 3.14, Click, psycopg, Pydantic AI, pytest, ruff, basedpyright. Follow project TDD: write the failing test, implement minimally, run `uv run ruff check --fix && uv run basedpyright`, commit.

---

## File Map

| Action | Path | Purpose |
| ------ | ---- | ------- |
| Create | `src/mailpilot/email_ops.py` | New policy layer: `send_email`, `reply_email`, `EmailOpsError` hierarchy. |
| Create | `tests/test_email_ops.py` | Integration tests for `email_ops` (real DB). |
| Modify | `src/mailpilot/agent/tools.py` | Shrink `send_email` / `reply_email` to thin wrappers around `email_ops`. |
| Modify | `tests/test_agent_tools.py` | Trim duplicate scenarios; keep one test per exception->dict mapping. |
| Modify | `src/mailpilot/cli.py` | `email send` drops `--thread-id` / `--contact-id`; new `email reply` command. |
| Modify | `tests/test_cli.py` | Drop `--thread-id` / `--contact-id` cases; add `email send` guard tests; add `email reply` tests. |
| Modify | `CLAUDE.md` | Update CLI command listing. |

---

## Task 1: Create `email_ops` skeleton with exception hierarchy

**Files:**
- Create: `src/mailpilot/email_ops.py`
- Test: `tests/test_email_ops.py`

- [ ] **Step 1.1: Write failing test for the exception hierarchy**

Create `tests/test_email_ops.py`:

```python
"""Tests for the email_ops policy layer."""

from __future__ import annotations

from mailpilot.email_ops import (
    ContactDisabled,
    ContactMissing,
    Cooldown,
    EmailOpsError,
    OriginalMissingContact,
    OriginalMissingThread,
    OriginalNotFound,
)


def test_exception_codes_match_agent_tool_strings() -> None:
    """`code` attributes must match the strings the agent tool returned
    historically, so the LLM-facing error contract is preserved."""
    assert ContactDisabled.code == "contact_disabled"
    assert Cooldown.code == "cooldown"
    assert OriginalNotFound.code == "not_found"
    assert OriginalMissingThread.code == "no_thread"
    assert OriginalMissingContact.code == "no_contact"
    assert ContactMissing.code == "not_found"


def test_exceptions_inherit_from_email_ops_error() -> None:
    for cls in (
        ContactDisabled,
        Cooldown,
        OriginalNotFound,
        OriginalMissingThread,
        OriginalMissingContact,
        ContactMissing,
    ):
        assert issubclass(cls, EmailOpsError)


def test_exception_str_carries_message() -> None:
    exc = ContactDisabled("contact is bounced: hard fail")
    assert str(exc) == "contact is bounced: hard fail"
    assert exc.code == "contact_disabled"
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run pytest tests/test_email_ops.py -v`
Expected: ImportError (`mailpilot.email_ops` does not exist).

- [ ] **Step 1.3: Create `src/mailpilot/email_ops.py` with the hierarchy**

```python
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


class ContactDisabled(EmailOpsError):
    code = "contact_disabled"


class Cooldown(EmailOpsError):
    code = "cooldown"


class OriginalNotFound(EmailOpsError):
    code = "not_found"


class OriginalMissingThread(EmailOpsError):
    code = "no_thread"


class OriginalMissingContact(EmailOpsError):
    code = "no_contact"


class ContactMissing(EmailOpsError):
    code = "not_found"
```

- [ ] **Step 1.4: Run tests + lint + types**

Run: `uv run pytest tests/test_email_ops.py -v && uv run ruff check --fix && uv run basedpyright`
Expected: 3 passing tests, no lint errors, no type errors.

- [ ] **Step 1.5: Commit**

```bash
git add src/mailpilot/email_ops.py tests/test_email_ops.py
git commit -m "feat(email_ops): add EmailOpsError exception hierarchy"
```

---

## Task 2: Move `send_email` policy into `email_ops`

**Files:**
- Modify: `src/mailpilot/email_ops.py`
- Test: `tests/test_email_ops.py`

The new `email_ops.send_email` mirrors today's `agent.tools.send_email` but returns the `Email` row directly and raises typed exceptions instead of returning error dicts. The agent-tool wrapper in Task 3 keeps the existing dict contract.

- [ ] **Step 2.1: Write failing tests for `email_ops.send_email`**

Append to `tests/test_email_ops.py`:

```python
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.database import (
    activate_workflow,
    create_enrollment,
    disable_contact as db_disable_contact,
    get_enrollment,
    update_workflow,
)
from mailpilot.email_ops import send_email
from mailpilot.models import Account, Email


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    update_workflow(
        connection,
        workflow_id,
        objective="Test objective",
        instructions="Test instructions",
    )
    activate_workflow(connection, workflow_id)


def _make_gmail_client(account: Account) -> MagicMock:
    client = MagicMock()
    client.send_message.return_value = {
        "id": "gmail-msg-1",
        "threadId": "gmail-thread-1",
        "labelIds": ["SENT"],
    }
    return client


def test_send_email_returns_email_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Hi there",
        workflow_id=workflow.id,
    )

    assert isinstance(email, Email)
    assert email.gmail_message_id == "gmail-msg-1"
    assert email.gmail_thread_id == "gmail-thread-1"
    gmail_client.send_message.assert_called_once()


def test_send_email_unknown_contact_succeeds(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """No contact row -> no guards fire, send proceeds."""
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="brand-new@example.com",
        subject="Hi",
        body="Body",
        workflow_id=workflow.id,
    )
    assert email.gmail_message_id == "gmail-msg-1"


def test_send_email_raises_contact_disabled_when_bounced(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    db_disable_contact(database_connection, contact.id, "bounced", "hard fail")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    with pytest.raises(ContactDisabled) as excinfo:
        send_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            to="recipient@example.com",
            subject="Hello",
            body="Hi",
            workflow_id=workflow.id,
        )
    assert "bounced" in str(excinfo.value)
    gmail_client.send_message.assert_not_called()


def test_send_email_raises_cooldown_when_recent_cold_send(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    from mailpilot.database import create_email

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    # Prior cold outbound 5 days ago.
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Earlier",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="prior-1",
        gmail_thread_id="prior-thread-1",
        sent_at=datetime.now(UTC) - timedelta(days=5),
    )
    gmail_client = _make_gmail_client(account)

    with pytest.raises(Cooldown) as excinfo:
        send_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            to="recipient@example.com",
            subject="Hello",
            body="Hi",
            workflow_id=workflow.id,
        )
    assert "cooldown" in str(excinfo.value).lower()
    gmail_client.send_message.assert_not_called()


def test_send_email_activates_pending_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    gmail_client = _make_gmail_client(account)

    send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Hi",
        workflow_id=workflow.id,
    )

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"


def test_send_email_no_workflow_id_skips_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """workflow_id=None is allowed (CLI ad-hoc send); no enrollment touched."""
    account = make_test_account(database_connection)
    make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )
    gmail_client = _make_gmail_client(account)

    email = send_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        to="recipient@example.com",
        subject="Hello",
        body="Hi",
    )
    assert email.gmail_message_id == "gmail-msg-1"
```

Add the missing exception import at the top of the file alongside the existing ones (`ContactDisabled`, `Cooldown`).

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_email_ops.py -v`
Expected: ImportError on `from mailpilot.email_ops import send_email` (the function does not exist yet).

- [ ] **Step 2.3: Implement `email_ops.send_email`**

Append to `src/mailpilot/email_ops.py`:

```python
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from mailpilot import database
from mailpilot.models import Account, Email
from mailpilot.settings import Settings
from mailpilot.sync import GmailClient
from mailpilot.sync import send_email as sync_send_email

_COOLDOWN_DAYS = 30


def _activate_enrollment_if_pending(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    enrollment = database.get_enrollment(connection, workflow_id, contact_id)
    if enrollment is not None and enrollment.status == "pending":
        database.update_enrollment(
            connection,
            workflow_id,
            contact_id,
            status="active",
            reason="email sent",
        )


def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    to: str,
    subject: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Send a new outbound email. See module docstring for guards.

    Auto-resolves contact_id from ``to``. If the contact exists, applies
    contact-status and 30-day cold-outbound cooldown guards. Activates a
    pending enrollment when both ``workflow_id`` and a resolved contact
    are present.

    Raises:
        ContactDisabled: contact is bounced/unsubscribed.
        Cooldown: prior unsolicited send within 30 days.
    """
    contact = database.get_contact_by_email(connection, to)
    contact_id: str | None = None
    if contact is not None:
        contact_id = contact.id
        if contact.status != "active":
            raise ContactDisabled(
                f"contact is {contact.status}: {contact.status_reason}"
            )
        last = database.get_last_cold_outbound(
            connection, account.id, contact.id, workflow_id
        )
        if last is not None and last.created_at > datetime.now(UTC) - timedelta(
            days=_COOLDOWN_DAYS
        ):
            raise Cooldown(
                f"last unsolicited email sent {last.created_at.isoformat()}; "
                f"cooldown is {_COOLDOWN_DAYS} days"
            )

    email = sync_send_email(
        connection=connection,
        account=account,
        gmail_client=gmail_client,
        settings=settings,
        to=to,
        subject=subject,
        body=body,
        contact_id=contact_id,
        workflow_id=workflow_id,
        cc=cc,
        bcc=bcc,
    )

    if workflow_id is not None and contact_id is not None:
        _activate_enrollment_if_pending(connection, workflow_id, contact_id)

    return email
```

- [ ] **Step 2.4: Run tests, lint, types**

Run: `uv run pytest tests/test_email_ops.py -v && uv run ruff check --fix && uv run basedpyright`
Expected: all 6 new + 3 prior tests pass; no lint or type errors.

- [ ] **Step 2.5: Commit**

```bash
git add src/mailpilot/email_ops.py tests/test_email_ops.py
git commit -m "feat(email_ops): move send_email policy into shared module"
```

---

## Task 3: Refactor `agent.tools.send_email` to thin wrapper

The existing agent-tool tests assert on the dict contract; they keep passing because the wrapper preserves it. The detailed scenario coverage is now in `test_email_ops.py`, so we trim `test_agent_tools.py` to one test per exception->dict mapping.

**Files:**
- Modify: `src/mailpilot/agent/tools.py`
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 3.1: Run the existing agent-tool send tests to confirm green baseline**

Run: `uv run pytest tests/test_agent_tools.py -v -k send_email`
Expected: all current `test_send_email_*` tests pass.

- [ ] **Step 3.2: Replace `agent.tools.send_email` body**

In `src/mailpilot/agent/tools.py`, replace the entire `send_email` function (currently lines ~65-150) with:

```python
def send_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Agent tool: send a new outbound email via Gmail.

    Thin wrapper over :func:`mailpilot.email_ops.send_email`. Converts
    typed policy exceptions into the LLM-facing error dict shape.
    """
    from mailpilot import email_ops

    try:
        email = email_ops.send_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            to=to,
            subject=subject,
            body=body,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}

    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }
```

Also remove the `_activate_enrollment_if_pending` helper (lines ~39-62) and the `_COOLDOWN_DAYS` constant (line 36) -- both now live in `email_ops`. Drop now-unused imports (`UTC`, `datetime`, `timedelta`, and `sync_send_email`) if no other tool in the same file still needs them. Re-check after editing `reply_email` in Task 5; for this task it is fine to keep them temporarily.

- [ ] **Step 3.3: Run all agent-tool tests**

Run: `uv run pytest tests/test_agent_tools.py -v -k send_email`
Expected: all `test_send_email_*` tests still pass on the unchanged dict contract.

- [ ] **Step 3.4: Trim duplicated scenarios from `test_agent_tools.py`**

In `tests/test_agent_tools.py`, the following tests duplicate scenarios now covered in `test_email_ops.py`. Delete them:

- `test_send_email_unknown_contact_succeeds`
- `test_send_email_passes_cc_and_bcc`
- `test_send_email_activates_pending_contact`
- `test_send_email_does_not_change_non_pending_status`
- `test_send_email_no_error_without_enrollment`

Keep these (they verify the wrapper's exception->dict mapping):

- `test_send_email_success`
- `test_send_email_blocked_by_contact_status`
- `test_send_email_blocked_by_cooldown`

If you removed the only consumer of an import (e.g., `create_enrollment`, `get_enrollment`), drop the import too.

- [ ] **Step 3.5: Run full lint + types + tests**

Run: `uv run ruff check --fix && uv run basedpyright && uv run pytest tests/test_agent_tools.py tests/test_email_ops.py -v`
Expected: clean.

- [ ] **Step 3.6: Commit**

```bash
git add src/mailpilot/agent/tools.py tests/test_agent_tools.py
git commit -m "refactor(agent): make send_email tool a wrapper over email_ops"
```

---

## Task 4: Move `reply_email` policy into `email_ops`

**Files:**
- Modify: `src/mailpilot/email_ops.py`
- Modify: `tests/test_email_ops.py`

- [ ] **Step 4.1: Write failing tests**

Append to `tests/test_email_ops.py`:

```python
from mailpilot.email_ops import reply_email
from mailpilot.database import create_email


def _make_inbound(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
    workflow_id: str,
    subject: str = "Question about pricing",
    rfc2822_message_id: str | None = "<inbound-1@example.com>",
):
    inbound = create_email(
        connection,
        account_id=account_id,
        direction="inbound",
        subject=subject,
        contact_id=contact_id,
        workflow_id=workflow_id,
        gmail_message_id="inbound-msg-1",
        gmail_thread_id="thread-abc",
        rfc2822_message_id=rfc2822_message_id,
    )
    assert inbound is not None
    return inbound


def test_reply_email_resolves_thread_recipient_and_subject(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    gmail_client = _make_gmail_client(account)

    email = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="Here is the pricing info.",
        workflow_id=workflow.id,
    )

    assert email.gmail_message_id == "gmail-msg-1"
    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == "sender@example.com"
    assert call_kwargs["subject"] == "Re: Question about pricing"
    assert call_kwargs["thread_id"] == "thread-abc"


def test_reply_email_preserves_existing_re_prefix(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection, account.id, contact.id, workflow.id, subject="Re: Pricing"
    )
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="More info",
        workflow_id=workflow.id,
    )

    assert gmail_client.send_message.call_args.kwargs["subject"] == "Re: Pricing"


def test_reply_email_raises_original_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalNotFound):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id="nonexistent",
            body="hi",
            workflow_id=workflow.id,
        )
    gmail_client.send_message.assert_not_called()


def test_reply_email_raises_missing_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="No thread",
        contact_id=contact.id,
        workflow_id=workflow.id,
        gmail_message_id="m-1",
        gmail_thread_id=None,
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalMissingThread):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_raises_missing_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="No contact",
        contact_id=None,
        workflow_id=workflow.id,
        gmail_message_id="m-2",
        gmail_thread_id="t-2",
    )
    assert inbound is not None
    gmail_client = _make_gmail_client(account)

    with pytest.raises(OriginalMissingContact):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_raises_contact_disabled(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    import pytest

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    db_disable_contact(database_connection, contact.id, "unsubscribed", "user opt-out")
    gmail_client = _make_gmail_client(account)

    with pytest.raises(ContactDisabled):
        reply_email(
            connection=database_connection,
            account=account,
            gmail_client=gmail_client,
            settings=make_test_settings(),
            email_id=inbound.id,
            body="hi",
            workflow_id=workflow.id,
        )


def test_reply_email_passes_in_reply_to_kwarg(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """The original's rfc2822_message_id is forwarded as in_reply_to to
    sync.send_email so threading headers are emitted."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    inbound = _make_inbound(
        database_connection,
        account.id,
        contact.id,
        workflow.id,
        rfc2822_message_id="<CABx-orig@mail.gmail.com>",
    )
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="hi",
        workflow_id=workflow.id,
    )

    assert (
        gmail_client.send_message.call_args.kwargs["in_reply_to"]
        == "<CABx-orig@mail.gmail.com>"
    )


def test_reply_email_activates_pending_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    inbound = _make_inbound(database_connection, account.id, contact.id, workflow.id)
    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        email_id=inbound.id,
        body="hi",
        workflow_id=workflow.id,
    )

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"
```

- [ ] **Step 4.2: Run failing tests**

Run: `uv run pytest tests/test_email_ops.py -v -k reply_email`
Expected: ImportError (`reply_email` missing from `email_ops`).

- [ ] **Step 4.3: Implement `email_ops.reply_email`**

Append to `src/mailpilot/email_ops.py`:

```python
def reply_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: GmailClient,
    settings: Settings,
    *,
    email_id: str,
    body: str,
    workflow_id: str | None = None,
    cc: str | None = None,
    bcc: str | None = None,
) -> Email:
    """Reply to an existing email in-thread.

    Auto-derives recipient (contact.email), subject ("Re: " prefixed
    unless already prefixed), thread_id, and In-Reply-To from the
    original. No cooldown -- replies are always allowed. Activates a
    matching pending enrollment when ``workflow_id`` is set.

    Raises:
        OriginalNotFound: ``email_id`` does not exist.
        OriginalMissingThread: original has no ``gmail_thread_id``.
        OriginalMissingContact: original has no ``contact_id``.
        ContactMissing: ``original.contact_id`` does not resolve.
        ContactDisabled: contact is bounced/unsubscribed.
    """
    original = database.get_email(connection, email_id)
    if original is None:
        raise OriginalNotFound(f"email not found: {email_id}")
    if original.gmail_thread_id is None:
        raise OriginalMissingThread(f"email has no gmail_thread_id: {email_id}")
    if original.contact_id is None:
        raise OriginalMissingContact(f"email has no contact_id: {email_id}")

    contact = database.get_contact(connection, original.contact_id)
    if contact is None:
        raise ContactMissing(f"contact not found: {original.contact_id}")
    if contact.status != "active":
        raise ContactDisabled(
            f"contact is {contact.status}: {contact.status_reason}"
        )

    subject = original.subject
    if not subject.lower().startswith("re: "):
        subject = f"Re: {subject}"

    email = sync_send_email(
        connection=connection,
        account=account,
        gmail_client=gmail_client,
        settings=settings,
        to=contact.email,
        subject=subject,
        body=body,
        contact_id=contact.id,
        workflow_id=workflow_id,
        thread_id=original.gmail_thread_id,
        cc=cc,
        bcc=bcc,
        in_reply_to=original.rfc2822_message_id,
    )

    if workflow_id is not None:
        _activate_enrollment_if_pending(connection, workflow_id, contact.id)

    return email
```

- [ ] **Step 4.4: Run tests, lint, types**

Run: `uv run pytest tests/test_email_ops.py -v && uv run ruff check --fix && uv run basedpyright`
Expected: all `email_ops` tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add src/mailpilot/email_ops.py tests/test_email_ops.py
git commit -m "feat(email_ops): move reply_email policy into shared module"
```

---

## Task 5: Refactor `agent.tools.reply_email` to thin wrapper

**Files:**
- Modify: `src/mailpilot/agent/tools.py`
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 5.1: Replace `agent.tools.reply_email` body**

In `src/mailpilot/agent/tools.py`, replace the entire `reply_email` function with:

```python
def reply_email(  # noqa: PLR0913
    connection: psycopg.Connection[dict[str, Any]],
    account: Account,
    gmail_client: object,
    settings: Settings,
    workflow_id: str,
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Agent tool: reply in-thread. Wraps :func:`email_ops.reply_email`.

    Converts typed policy exceptions into the LLM-facing error dict.
    """
    from mailpilot import email_ops

    try:
        email = email_ops.reply_email(
            connection,
            account,
            gmail_client,  # type: ignore[arg-type]
            settings,
            email_id=email_id,
            body=body,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
    except email_ops.EmailOpsError as exc:
        return {"error": exc.code, "message": str(exc)}

    return {
        "id": email.id,
        "gmail_message_id": email.gmail_message_id,
        "gmail_thread_id": email.gmail_thread_id,
    }
```

Now `agent/tools.py` no longer references `_activate_enrollment_if_pending`, `_COOLDOWN_DAYS`, `sync_send_email`, `UTC`, `datetime`, or `timedelta`. Remove them.

- [ ] **Step 5.2: Trim duplicated `reply_email` scenarios from `test_agent_tools.py`**

Delete these tests (now covered in `test_email_ops.py`):

- `test_reply_email_preserves_existing_re_prefix`
- `test_reply_email_no_thread_id`
- `test_reply_email_no_contact`
- `test_reply_email_activates_pending_contact`
- `test_reply_email_does_not_change_non_pending_status`
- `test_reply_email_no_error_without_enrollment`
- `test_reply_email_passes_in_reply_to_from_original`
- `test_reply_email_omits_in_reply_to_when_original_has_no_message_id`

Keep these (they verify the wrapper's exception->dict mapping):

- `test_reply_email_resolves_thread_and_recipient`
- `test_reply_email_not_found`
- `test_reply_email_blocked_contact`

Drop now-unused imports.

- [ ] **Step 5.3: Run full check**

Run: `uv run ruff check --fix && uv run basedpyright && uv run pytest tests/test_agent_tools.py tests/test_email_ops.py -v`
Expected: clean.

- [ ] **Step 5.4: Commit**

```bash
git add src/mailpilot/agent/tools.py tests/test_agent_tools.py
git commit -m "refactor(agent): make reply_email tool a wrapper over email_ops"
```

---

## Task 6: Refactor CLI `email send` to call `email_ops`

`email send` drops `--contact-id` and `--thread-id`, applies guards via `email_ops`, and translates `EmailOpsError` to `output_error`.

**Files:**
- Modify: `src/mailpilot/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 6.1: Update existing CLI send tests for the new shape**

In `tests/test_cli.py`:

1. **Delete** `test_email_send_with_optional_flags` (it asserts on `--contact-id` / `--thread-id`, which are removed).
2. In all remaining `test_email_send_*` tests, replace `patch("mailpilot.sync.send_email", ...)` with `patch("mailpilot.email_ops.send_email", ...)`. Update the kwargs assertions: drop `contact_id` and `thread_id` references; keep `to`, `subject`, `body`, `cc`, `bcc`, `workflow_id`.

3. **Add** new tests for guard mapping. After the existing `test_email_send_*` block, append:

```python
def test_email_send_contact_disabled_returns_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import ContactDisabled

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.send_email",
            side_effect=ContactDisabled("contact is bounced: hard fail"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "r@example.com",
                "--subject",
                "s",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["ok"] is False
    assert data["error"] == "contact_disabled"
    assert "bounced" in data["message"]


def test_email_send_cooldown_returns_error(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import Cooldown

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.send_email",
            side_effect=Cooldown("last unsolicited email sent ...; cooldown is 30 days"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "send",
                "--account-id",
                account.id,
                "--to",
                "r@example.com",
                "--subject",
                "s",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "cooldown"
```

- [ ] **Step 6.2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v -k email_send`
Expected: failures referencing `mailpilot.email_ops.send_email` not being callable from the CLI yet, plus the new guard tests failing because the CLI still calls `sync.send_email`.

- [ ] **Step 6.3: Update `cli.py` `email send` command**

In `src/mailpilot/cli.py`, replace the `@email.command("send")` block (currently lines ~788-855) with:

```python
@email.command("send")
@click.option("--account-id", required=True, help="Sending account ID.")
@click.option(
    "--to",
    "to",
    required=True,
    multiple=True,
    help="Recipient email address (repeatable).",
)
@click.option("--subject", required=True, help="Email subject.")
@click.option("--body", required=True, help="Plain text body.")
@click.option("--workflow-id", default=None, help="Link to a workflow.")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_send(
    account_id: str,
    to: tuple[str, ...],
    subject: str,
    body: str,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Send a new outbound email via the given account's Gmail mailbox.

    Use ``email reply`` to continue an existing thread.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.database import get_account, get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not subject.strip():
        output_error("subject cannot be empty", "validation_error")
    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    to_joined = ",".join(to)
    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        account = get_account(connection, account_id)
        if account is None:
            output_error(f"account not found: {account_id}", "not_found")
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        client = GmailClient(account.email)
        try:
            sent = email_ops.send_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                to=to_joined,
                subject=subject,
                body=body,
                workflow_id=workflow_id,
                cc=cc,
                bcc=bcc,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.send.failed", account_id=account.id, to=to
            )
            output_error(str(exc), "send_failed")
        output(sent.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 6.4: Run all CLI send tests**

Run: `uv run pytest tests/test_cli.py -v -k email_send`
Expected: all pass (existing + new guard tests).

- [ ] **Step 6.5: Run full check**

Run: `uv run ruff check --fix && uv run basedpyright && uv run pytest -x`
Expected: clean.

- [ ] **Step 6.6: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "refactor(cli): route email send through email_ops, drop --thread-id and --contact-id"
```

---

## Task 7: Add CLI `email reply` command

**Files:**
- Modify: `src/mailpilot/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 7.1: Write failing tests**

Append to `tests/test_cli.py` (at the end of the email-section, after the email send tests):

```python
# -- email reply ---------------------------------------------------------------


def test_email_reply_success(runner: CliRunner, mock_connection: MagicMock) -> None:
    account = _make_account()
    sent = _make_email(
        direction="outbound",
        status="sent",
        subject="Re: Hi",
        body_text="Reply body",
        gmail_message_id="gm-2",
        gmail_thread_id="gt-1",
        sent_at=_NOW,
    )
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient") as mock_client_cls,
        patch("mailpilot.email_ops.reply_email", return_value=sent) as mock_reply,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "original-email-1",
                "--body",
                "Reply body",
            ],
        )

    assert result.exit_code == 0, result.output
    mock_client_cls.assert_called_once_with(account.email)
    kwargs = mock_reply.call_args.kwargs
    assert kwargs["email_id"] == "original-email-1"
    assert kwargs["body"] == "Reply body"
    assert kwargs["workflow_id"] is None
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["id"] == sent.id


def test_email_reply_account_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=None),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                "missing",
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_email_reply_empty_body_rejected(runner: CliRunner) -> None:
    result = runner.invoke(
        main,
        [
            "email",
            "reply",
            "--account-id",
            "a",
            "--email-id",
            "e",
            "--body",
            "   ",
        ],
    )
    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "validation_error"


def test_email_reply_original_not_found(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import OriginalNotFound

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.reply_email",
            side_effect=OriginalNotFound("email not found: x"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "not_found"


def test_email_reply_contact_disabled(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    from mailpilot.email_ops import ContactDisabled

    account = _make_account()
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.gmail.GmailClient"),
        patch(
            "mailpilot.email_ops.reply_email",
            side_effect=ContactDisabled("contact is bounced: hard fail"),
        ),
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "x",
                "--body",
                "b",
            ],
        )

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["error"] == "contact_disabled"


def test_email_reply_with_workflow_id(
    runner: CliRunner, mock_connection: MagicMock
) -> None:
    account = _make_account()
    workflow = _make_workflow(account_id=account.id)
    sent = _make_email(direction="outbound", status="sent", sent_at=_NOW)
    with (
        patch("mailpilot.settings.get_settings", return_value=make_test_settings()),
        patch("mailpilot.database.initialize_database", return_value=mock_connection),
        patch("mailpilot.database.get_account", return_value=account),
        patch("mailpilot.database.get_workflow", return_value=workflow),
        patch("mailpilot.gmail.GmailClient"),
        patch("mailpilot.email_ops.reply_email", return_value=sent) as mock_reply,
    ):
        result = runner.invoke(
            main,
            [
                "email",
                "reply",
                "--account-id",
                account.id,
                "--email-id",
                "original-1",
                "--body",
                "hi",
                "--workflow-id",
                workflow.id,
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_reply.call_args.kwargs["workflow_id"] == workflow.id
```

`_make_workflow` already exists at `tests/test_cli.py:1588`.

- [ ] **Step 7.2: Run failing tests**

Run: `uv run pytest tests/test_cli.py -v -k email_reply`
Expected: all six fail because `email reply` is not registered.

- [ ] **Step 7.3: Add `email reply` to `cli.py`**

In `src/mailpilot/cli.py`, immediately after the `email_send` function add:

```python
@email.command("reply")
@click.option("--account-id", required=True, help="Sending account ID.")
@click.option(
    "--email-id",
    required=True,
    help="ID of the email being replied to.",
)
@click.option("--body", required=True, help="Reply body (plain text).")
@click.option("--workflow-id", default=None, help="Link to a workflow.")
@click.option("--cc", default=None, help="CC recipient(s), comma-separated.")
@click.option("--bcc", default=None, help="BCC recipient(s), comma-separated.")
def email_reply(
    account_id: str,
    email_id: str,
    body: str,
    workflow_id: str | None,
    cc: str | None,
    bcc: str | None,
) -> None:
    """Reply to an existing email in-thread.

    Auto-derives recipient, subject (with "Re: " prefix), thread, and
    In-Reply-To from the original. No cooldown applied.
    """
    import logfire

    from mailpilot import email_ops
    from mailpilot.database import get_account, get_workflow, initialize_database
    from mailpilot.gmail import GmailClient
    from mailpilot.settings import get_settings

    if not body.strip():
        output_error("body cannot be empty", "validation_error")

    settings = get_settings()
    connection = initialize_database(_database_url())
    try:
        account = get_account(connection, account_id)
        if account is None:
            output_error(f"account not found: {account_id}", "not_found")
        if workflow_id is not None and get_workflow(connection, workflow_id) is None:
            output_error(f"workflow not found: {workflow_id}", "not_found")
        client = GmailClient(account.email)
        try:
            sent = email_ops.reply_email(
                connection,
                account=account,
                gmail_client=client,
                settings=settings,
                email_id=email_id,
                body=body,
                workflow_id=workflow_id,
                cc=cc,
                bcc=bcc,
            )
        except email_ops.EmailOpsError as exc:
            output_error(str(exc), exc.code)
        except Exception as exc:
            logfire.exception(
                "cli.email.reply.failed",
                account_id=account.id,
                email_id=email_id,
            )
            output_error(str(exc), "send_failed")
        output(sent.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 7.4: Run all CLI tests**

Run: `uv run pytest tests/test_cli.py -v -k email_`
Expected: all pass.

- [ ] **Step 7.5: Run full check**

Run: `uv run ruff check --fix && uv run basedpyright && uv run pytest -x`
Expected: clean.

- [ ] **Step 7.6: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "feat(cli): add email reply command"
```

---

## Task 8: Update CLAUDE.md command listing

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 8.1: Replace the `email send` line in the CLI listing**

In `CLAUDE.md`, find the line:

```
mailpilot email send --account-id ID --to E [--to E2 ...] --subject S --body B [--contact-id ID] [--workflow-id ID] [--thread-id ID] [--cc E] [--bcc E]
```

Replace with:

```
mailpilot email send  --account-id ID --to E [--to E2 ...] --subject S --body B [--workflow-id ID] [--cc E] [--bcc E]
mailpilot email reply --account-id ID --email-id ID --body B [--workflow-id ID] [--cc E] [--bcc E]
```

- [ ] **Step 8.2: Verify CLI help matches**

Run: `uv run mailpilot email --help` and `uv run mailpilot email send --help` and `uv run mailpilot email reply --help`.
Expected: flags match the CLAUDE.md listing exactly.

- [ ] **Step 8.3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(cli): update CLAUDE.md for email reply and unified email_ops"
```

---

## Task 9: Final verification

- [ ] **Step 9.1: `make check`**

Run: `make check`
Expected: format clean, lint clean, types clean, all tests pass.

- [ ] **Step 9.2: Manual sanity probe (no live Gmail)**

Run: `uv run mailpilot email reply --help`
Expected: help output lists `--account-id`, `--email-id`, `--body`, `--workflow-id`, `--cc`, `--bcc` and no `--to` / `--subject` / `--thread-id`.

Run: `uv run mailpilot email send --help`
Expected: no `--contact-id` and no `--thread-id`.

- [ ] **Step 9.3: Optional -- run smoke test**

If a tester wants e2e confirmation, run `/smoke-test`. This exercises the agent's reply path; it does not exercise the new CLI `email reply` command, but it confirms the agent wrappers still produce the same dict shape after refactor.

- [ ] **Step 9.4: Open the PR**

Use `/github-pr-create` with body referencing the spec at `docs/superpowers/specs/2026-04-26-email-reply-cli-design.md`.
