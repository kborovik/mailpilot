# reply_email Agent Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `reply_email` agent tool that replies to an existing email in-thread, resolving recipient, subject, and thread_id automatically from the original email.

**Architecture:** New `reply_email` tool function in `agent/tools.py` that takes `email_id` + `body` (+ optional `cc`/`bcc`), looks up the original email and its contact, derives `to`/`subject`/`thread_id`, then delegates to `sync.send_email`. Wrapper added to `agent/invoke.py` and registered in `_TOOLS`. Trigger prompt updated to show `Email ID` instead of `Thread ID`.

**Tech Stack:** Python, Pydantic AI tools, psycopg, pytest

---

### Task 1: Add `reply_email` to `agent/tools.py`

**Files:**
- Modify: `src/mailpilot/agent/tools.py`
- Test: `tests/test_agent_tools.py`

- [ ] **Step 1: Write the failing test for basic reply**

In `tests/test_agent_tools.py`, add after the `send_email` test section:

```python
# -- reply_email ---------------------------------------------------------------


def test_reply_email_resolves_thread_and_recipient(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """reply_email looks up original email to resolve to, subject, and thread_id."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    # Create the inbound email being replied to.
    inbound = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Question about pricing",
        gmail_message_id="inbound-msg-1",
        gmail_thread_id="thread-abc",
        body_text="How much does it cost?",
    )

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="It costs $100/month.",
    )

    assert "error" not in result
    assert result["gmail_message_id"] == "gmail-msg-1"
    # Verify send_message was called with resolved values.
    gmail_client.send_message.assert_called_once()
    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == "sender@example.com"
    assert call_kwargs["subject"] == "Re: Question about pricing"
    assert call_kwargs["thread_id"] == "thread-abc"
```

Add `reply_email` to the imports at the top of the file:

```python
from mailpilot.agent.tools import (
    ...
    reply_email,
    ...
)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_tools.py::test_reply_email_resolves_thread_and_recipient -x`
Expected: FAIL with `ImportError: cannot import name 'reply_email'`

- [ ] **Step 3: Write the `reply_email` function**

In `src/mailpilot/agent/tools.py`, add after the `send_email` function (before `create_task`):

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
    """Reply to an existing email in-thread.

    Resolves recipient, subject, and thread_id from the original email.
    Only checks contact status (not cooldown, since replies are always allowed).

    Args:
        connection: Open database connection.
        account: Sending account.
        gmail_client: Gmail client scoped to account.
        settings: Application settings.
        workflow_id: Current workflow FK.
        email_id: ID of the email being replied to.
        body: Reply body (plain text).
        cc: CC recipient(s), comma-separated.
        bcc: BCC recipient(s), comma-separated.

    Returns:
        Dict with sent message details (id, gmail_message_id, gmail_thread_id),
        or error dict if email not found or contact blocked.
    """
    with logfire.span("agent.tool.reply_email", email_id=email_id, workflow_id=workflow_id):
        original = database.get_email(connection, email_id)
        if original is None:
            return {"error": "not_found", "message": f"email not found: {email_id}"}

        # Resolve recipient from the original email's contact.
        contact_id = original.contact_id
        if contact_id is None:
            return {
                "error": "no_contact",
                "message": "original email has no linked contact",
            }

        contact = database.get_contact(connection, contact_id)
        if contact is None:
            return {"error": "not_found", "message": f"contact not found: {contact_id}"}

        # Guard: contact status check (no cooldown -- replies always allowed).
        if contact.status != "active":
            return {
                "error": "contact_disabled",
                "message": f"contact is {contact.status}: {contact.status_reason}",
            }

        # Derive subject with Re: prefix.
        subject = original.subject
        if not subject.startswith("Re: "):
            subject = f"Re: {subject}"

        email = sync_send_email(
            connection=connection,
            account=account,
            gmail_client=gmail_client,  # type: ignore[arg-type]
            settings=settings,
            to=contact.email,
            subject=subject,
            body=body,
            contact_id=contact.id,
            workflow_id=workflow_id,
            thread_id=original.gmail_thread_id,
            cc=cc,
            bcc=bcc,
        )
        return {
            "id": email.id,
            "gmail_message_id": email.gmail_message_id,
            "gmail_thread_id": email.gmail_thread_id,
        }
```

Update the module docstring tool list to include `reply_email`:

```python
#    - ``reply_email`` -- reply to an existing email in-thread
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_tools.py::test_reply_email_resolves_thread_and_recipient -x`
Expected: PASS

- [ ] **Step 5: Write test for email not found**

```python
def test_reply_email_not_found(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    account = make_test_account(database_connection)
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id="nonexistent-id",
        body="Reply body",
    )

    assert result["error"] == "not_found"
    gmail_client.send_message.assert_not_called()
```

- [ ] **Step 6: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_tools.py::test_reply_email_not_found -x`
Expected: PASS

- [ ] **Step 7: Write test for blocked contact**

```python
def test_reply_email_blocked_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    from mailpilot.database import disable_contact as db_disable_contact

    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="bounced@example.com", domain="example.com"
    )
    db_disable_contact(database_connection, contact.id, "bounced", "hard bounce")
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    inbound = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Hello",
        gmail_message_id="inbound-blocked",
        gmail_thread_id="thread-blocked",
    )

    gmail_client = _make_gmail_client(account)

    result = reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Reply",
    )

    assert result["error"] == "contact_disabled"
    gmail_client.send_message.assert_not_called()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_tools.py::test_reply_email_blocked_contact -x`
Expected: PASS

- [ ] **Step 9: Write test for Re: prefix idempotency**

```python
def test_reply_email_preserves_existing_re_prefix(
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """reply_email does not double-add Re: prefix."""
    account = make_test_account(database_connection)
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)

    inbound = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Re: Original topic",
        gmail_message_id="inbound-re",
        gmail_thread_id="thread-re",
    )

    gmail_client = _make_gmail_client(account)

    reply_email(
        connection=database_connection,
        account=account,
        gmail_client=gmail_client,
        settings=make_test_settings(),
        workflow_id=workflow.id,
        email_id=inbound.id,
        body="Follow up",
    )

    call_kwargs = gmail_client.send_message.call_args.kwargs
    assert call_kwargs["subject"] == "Re: Original topic"
```

- [ ] **Step 10: Run all reply_email tests**

Run: `uv run pytest tests/test_agent_tools.py -k reply_email -v`
Expected: 4 PASS

- [ ] **Step 11: Commit**

```bash
git add src/mailpilot/agent/tools.py tests/test_agent_tools.py
git commit -m "feat(agent): add reply_email tool with auto-resolved threading (#59)"
```

---

### Task 2: Add `reply_email` wrapper to `agent/invoke.py` and register in `_TOOLS`

**Files:**
- Modify: `src/mailpilot/agent/invoke.py`
- Test: `tests/test_agent_invoke.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_agent_invoke.py`, add a test that invokes the agent with a FunctionModel that calls the `reply_email` tool:

```python
def test_agent_calls_reply_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent can call reply_email tool to reply in-thread."""
    account, contact, workflow = _setup(database_connection)
    update_workflow(database_connection, workflow.id, type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    inbound = create_email(
        database_connection,
        gmail_message_id="msg-reply-invoke",
        gmail_thread_id="thread-reply-invoke",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Need help",
        body_text="Can you assist?",
    )

    model = _model_that_calls_tool(
        "reply_email",
        {"email_id": inbound.id, "body": "Sure, happy to help!"},
    )
    with patch("mailpilot.agent.invoke.GmailClient") as mock_cls:
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-1",
            "threadId": "thread-reply-invoke",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=inbound,
            model_override=model,
        )

    assert result is not None
    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == contact.email
    assert call_kwargs["subject"] == "Re: Need help"
    assert call_kwargs["thread_id"] == "thread-reply-invoke"
```

Add `MagicMock` to the imports if not already present:

```python
from unittest.mock import MagicMock, patch
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_invoke.py::test_agent_calls_reply_email -x`
Expected: FAIL (tool `reply_email` not registered)

- [ ] **Step 3: Add the wrapper and register the tool**

In `src/mailpilot/agent/invoke.py`, add the wrapper after `_wrap_send_email`:

```python
def _wrap_reply_email(
    ctx: RunContext[AgentDeps],
    email_id: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Reply to an existing email in-thread."""
    return agent_tools.reply_email(
        connection=ctx.deps.connection,
        account=ctx.deps.account,
        gmail_client=ctx.deps.gmail_client,
        settings=ctx.deps.settings,
        workflow_id=ctx.deps.workflow_id,
        email_id=email_id,
        body=body,
        cc=cc,
        bcc=bcc,
    )
```

Add to `_TOOLS` list after the `send_email` entry:

```python
Tool(_wrap_reply_email, name="reply_email"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_invoke.py::test_agent_calls_reply_email -x`
Expected: PASS

- [ ] **Step 5: Run all invoke tests**

Run: `uv run pytest tests/test_agent_invoke.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/invoke.py tests/test_agent_invoke.py
git commit -m "feat(agent): register reply_email tool in agent invoke (#59)"
```

---

### Task 3: Update trigger prompt to show Email ID instead of Thread ID

**Files:**
- Modify: `src/mailpilot/agent/invoke.py` (lines 300-308, `_format_trigger`)
- Test: `tests/test_agent_invoke.py`

- [ ] **Step 1: Update the existing `test_inbound_email_trigger_includes_thread_id` test**

Rename the test and change assertion to check for email ID and sender instead of thread ID:

```python
def test_inbound_email_trigger_includes_email_id_and_sender(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Inbound email trigger includes email ID and sender so agent can reply."""
    account, contact, workflow = _setup(database_connection)
    update_workflow(database_connection, workflow.id, type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    email = create_email(
        database_connection,
        gmail_message_id="msg-thread-test",
        gmail_thread_id="thread-abc-123",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Re: proposal",
        body_text="Looks good, let's proceed.",
    )

    captured_messages: list[ModelMessage] = []
    with patch("mailpilot.agent.invoke.GmailClient"):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert email.id in all_text
    assert "lead@acme.com" in all_text
    # Thread ID should NOT be exposed -- reply_email resolves it internally.
    assert "thread-abc-123" not in all_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_invoke.py::test_inbound_email_trigger_includes_email_id_and_sender -x`
Expected: FAIL (prompt still shows Thread ID, not Email ID)

- [ ] **Step 3: Update `_format_trigger`**

In `src/mailpilot/agent/invoke.py`, replace the `_format_trigger` email branch:

```python
def _format_trigger(
    email: Email | None,
    task_description: str,
    task_context: dict[str, Any] | None,
    contact_email: str = "",
) -> str:
    """Format the trigger context section of the prompt."""
    if email is not None:
        header = f"\nNew inbound email:\nEmail ID: {email.id}\nFrom: {contact_email}"
        return f"{header}\nSubject: {email.subject}\nBody:\n{email.body_text}"
    if task_description:
        lines = ["\nDeferred task:", f"Description: {task_description}"]
        if task_context:
            lines.append(f"Context: {task_context}")
        return "\n".join(lines)
    return (
        "\nThis is an outbound invocation. "
        "Review the contact and email history, then take appropriate action."
    )
```

Update the call site in `_build_user_prompt` to pass `contact_email`:

```python
sections.append(_format_trigger(email, task_description, task_context, contact_email=contact.email))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_invoke.py::test_inbound_email_trigger_includes_email_id_and_sender -x`
Expected: PASS

- [ ] **Step 5: Run all invoke tests to verify no regressions**

Run: `uv run pytest tests/test_agent_invoke.py -v`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/invoke.py tests/test_agent_invoke.py
git commit -m "refactor(agent): show Email ID and sender in trigger prompt (#59)"
```

---

### Task 4: Remove `thread_id` from `send_email` agent tool and clean up

The `send_email` agent tool should be for new conversations only. The `thread_id` parameter is no longer needed there -- replies go through `reply_email`.

**Files:**
- Modify: `src/mailpilot/agent/tools.py`
- Modify: `src/mailpilot/agent/invoke.py`
- Test: `tests/test_agent_tools.py`
- Test: `tests/test_agent_invoke.py`

- [ ] **Step 1: Remove `thread_id` from agent `send_email` in `tools.py`**

Remove the `thread_id` parameter from the `send_email` function signature and the cooldown guard's `if thread_id is None:` branch (cooldown now always applies for `send_email`). Update the docstring to remove thread_id references and note that replies should use `reply_email`.

Updated `send_email` in `agent/tools.py`:

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
    """Send a new outbound email via Gmail API.

    For replies to existing emails, use ``reply_email`` instead.

    Guards:
    1. Contact must be active (not bounced/unsubscribed) -- hard block
    2. Cooldown: blocked if last unsolicited outbound to this contact from
       this account was within cooldown period (30 days)

    Args:
        connection: Open database connection.
        account: Sending account.
        gmail_client: Gmail client scoped to account.
        settings: Application settings.
        workflow_id: Current workflow FK.
        to: Recipient email address.
        subject: Email subject.
        body: Email body (plain text).
        cc: CC recipient(s), comma-separated.
        bcc: BCC recipient(s), comma-separated.

    Returns:
        Dict with sent message details (id, gmail_message_id, gmail_thread_id),
        or error dict if blocked by guard.
    """
    with logfire.span("agent.tool.send_email", to=to, workflow_id=workflow_id):
        # Guard 1: contact status check.
        contact = database.get_contact_by_email(connection, to)
        contact_id: str | None = None
        if contact is not None:
            contact_id = contact.id
            if contact.status != "active":
                return {
                    "error": "contact_disabled",
                    "message": f"contact is {contact.status}: {contact.status_reason}",
                }

            # Guard 2: cooldown.
            last = database.get_last_cold_outbound(
                connection, account.id, contact.id, workflow_id
            )
            if last is not None and last.created_at > datetime.now(UTC) - timedelta(
                days=_COOLDOWN_DAYS
            ):
                sent_at = last.created_at.isoformat()
                return {
                    "error": "cooldown",
                    "message": (
                        f"last unsolicited email sent {sent_at}; "
                        f"cooldown is {_COOLDOWN_DAYS} days"
                    ),
                }

        email = sync_send_email(
            connection=connection,
            account=account,
            gmail_client=gmail_client,  # type: ignore[arg-type]
            settings=settings,
            to=to,
            subject=subject,
            body=body,
            contact_id=contact_id,
            workflow_id=workflow_id,
            cc=cc,
            bcc=bcc,
        )
        return {
            "id": email.id,
            "gmail_message_id": email.gmail_message_id,
            "gmail_thread_id": email.gmail_thread_id,
        }
```

- [ ] **Step 2: Remove `thread_id` from `_wrap_send_email` in `invoke.py`**

```python
def _wrap_send_email(
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
    bcc: str | None = None,
) -> dict[str, Any]:
    """Send a new outbound email. For replies, use reply_email instead."""
    return agent_tools.send_email(
        connection=ctx.deps.connection,
        account=ctx.deps.account,
        gmail_client=ctx.deps.gmail_client,
        settings=ctx.deps.settings,
        workflow_id=ctx.deps.workflow_id,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
    )
```

- [ ] **Step 3: Update tests**

In `tests/test_agent_tools.py`:
- Remove `test_send_email_reply_bypasses_cooldown` (this behavior moved to `reply_email`)
- Remove the `thread_id` parameter from `test_send_email_passes_cc_and_bcc` if it had one (it doesn't)

In `tests/test_agent_invoke.py`:
- No changes needed (invoke tests don't pass thread_id to send_email)

- [ ] **Step 4: Run all agent tests**

Run: `uv run pytest tests/test_agent_tools.py tests/test_agent_invoke.py -v`
Expected: all PASS

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix && uv run basedpyright`
Expected: 0 errors

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/agent/tools.py src/mailpilot/agent/invoke.py tests/test_agent_tools.py
git commit -m "refactor(agent): remove thread_id from send_email, replies use reply_email (#59)"
```

---

### Task 5: Full verification and CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md tool list**

In the `Tools per ADR-03` or agent tools documentation section, add `reply_email` to the tool list if it appears in CLAUDE.md.

No CLI changes needed -- `reply_email` is an agent-only tool, not a CLI command. The CLI `email send` retains `--thread-id` for direct use.

- [ ] **Step 2: Run full verification**

Run: `make check`
Expected: all lint, types, and tests pass

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add reply_email to agent tool documentation (#59)"
```
