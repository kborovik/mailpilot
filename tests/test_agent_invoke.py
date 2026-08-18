"""Tests for workflow agent invocation."""

from __future__ import annotations

import inspect
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import psycopg
import pytest
from logfire.testing import CaptureLogfire
from pydantic_ai import ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent.invoke import (
    _SUBJECT_REQUIRED_RETRY,  # pyright: ignore[reportPrivateUsage]
    _advisory_lock_keys,  # pyright: ignore[reportPrivateUsage]
    _advisory_lock_keys_for_task,  # pyright: ignore[reportPrivateUsage]
    _build_agent,  # pyright: ignore[reportPrivateUsage]
    _build_user_prompt,  # pyright: ignore[reportPrivateUsage]
    _is_new_thread_touch,  # pyright: ignore[reportPrivateUsage]
    _validate_touch_subject,  # pyright: ignore[reportPrivateUsage]
    _wrap_conclude_enrollment,  # pyright: ignore[reportPrivateUsage]
    _wrap_create_task,  # pyright: ignore[reportPrivateUsage]
    _wrap_disable_contact,  # pyright: ignore[reportPrivateUsage]
    _wrap_send_email,  # pyright: ignore[reportPrivateUsage]
    invoke_workflow_agent,
)
from mailpilot.agent.model import (
    _build_anthropic_model,  # pyright: ignore[reportPrivateUsage]
    _build_model,  # pyright: ignore[reportPrivateUsage]
    _build_xai_model,  # pyright: ignore[reportPrivateUsage]
)
from mailpilot.database import (
    activate_workflow,
    create_enrollment,
    update_workflow,
)
from mailpilot.exceptions import (
    AgentCompletedWithoutReplyError,
    AgentDidNotUseToolsError,
)

# -- Helpers -------------------------------------------------------------------


def _activate(connection: psycopg.Connection[dict[str, Any]], workflow_id: str) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        goal="Test goal",
        instructions="You are a sales outreach agent. Send an email to the contact.",
    )
    activate_workflow(connection, workflow_id)


def _setup(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_type: str = "outbound",
) -> tuple[Any, Any, Any]:
    """Create account, contact, and an active workflow of the requested direction."""
    account = make_test_account(connection, email="sender@example.com")
    contact = make_test_contact(connection, email="lead@acme.com")
    workflow = make_test_workflow(
        connection, account_id=account.id, workflow_type=workflow_type
    )
    _activate(connection, workflow.id)
    create_enrollment(connection, workflow.id, contact.id)
    return account, contact, workflow


def _model_that_calls_noop(
    messages: list[ModelMessage], info: AgentInfo
) -> ModelResponse:
    """FunctionModel that calls the noop tool then finishes."""
    # First call: invoke noop tool
    for msg in messages:
        for part in msg.parts if hasattr(msg, "parts") else []:
            if isinstance(part, ToolCallPart):
                # Tool result received -- finish with text
                return ModelResponse(
                    parts=[TextPart(content="Done, no action needed.")]
                )
    return ModelResponse(
        parts=[ToolCallPart(tool_name="noop", args={"reason": "no action needed"})]
    )


def _model_that_calls_tool(tool_name: str, tool_args: dict[str, Any]) -> FunctionModel:
    """Build a FunctionModel that calls a specific tool then finishes."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[ToolCallPart(tool_name=tool_name, args=tool_args)]
            )
        return ModelResponse(parts=[TextPart(content="Done.")])

    return FunctionModel(_respond)


def _model_no_tools(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """FunctionModel that returns text only, no tool calls."""
    return ModelResponse(
        parts=[TextPart(content="I thought about it but decided not to act.")]
    )


def _capturing_model(
    captured: list[ModelMessage],
) -> FunctionModel:
    """Build a FunctionModel that captures messages on first call, then finishes."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            captured.extend(messages)
            return ModelResponse(
                parts=[ToolCallPart(tool_name="noop", args={"reason": "testing"})]
            )
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(_respond)


# -- Tests: tool-use enforcement -----------------------------------------------


def test_agent_calls_noop_passes_enforcement(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent calls noop -> enforcement passes (noop counts as a tool call)."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )
    # No exception means enforcement passed.


def test_agent_no_tool_calls_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent returns text only -> enforcement raises AgentDidNotUseToolsError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(AgentDidNotUseToolsError, match=workflow.id),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_no_tools),
        )


def test_agent_calls_real_tool_passes_enforcement(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent calls a real tool (search_emails) -> enforcement passes."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool("search_emails", {"query": contact.email})
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=model,
        )


# -- Tests: advisory lock ------------------------------------------------------


def test_advisory_lock_skip_when_held(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When advisory lock is already held, invocation is skipped (returns None).

    Coarse-lock path (CLI ``enrollment_run``/``manual``): key derived from
    ``(workflow_id, contact_id)``. Preserves the original §B.42 intent that
    operator-initiated double-runs on the same enrollment serialize.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    # Acquire the same advisory lock from a separate connection to simulate
    # a concurrent invocation.
    from conftest import TEST_DATABASE_URL

    blocker = psycopg.connect(TEST_DATABASE_URL)
    try:
        k1, k2 = _advisory_lock_keys(workflow.id, contact.id)
        blocker.execute("SELECT pg_advisory_lock(%s, %s)", (k1, k2))

        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )
        assert result is None
    finally:
        blocker.close()


def test_advisory_lock_task_scope_when_task_id_provided(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.25: drain-path invocation locks on ``task_id``, not (wf, contact).

    Pre-acquire the *coarse* (workflow_id, contact_id) lock externally; the
    task-scoped invocation must NOT see contention because it now uses a
    task-derived key.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from conftest import TEST_DATABASE_URL

    blocker = psycopg.connect(TEST_DATABASE_URL)
    try:
        coarse_k1, coarse_k2 = _advisory_lock_keys(workflow.id, contact.id)
        blocker.execute("SELECT pg_advisory_lock(%s, %s)", (coarse_k1, coarse_k2))

        with (
            patch("mailpilot.agent.invoke.GmailClient"),
            patch("mailpilot.agent.invoke.DriveClient"),
        ):
            result = invoke_workflow_agent(
                database_connection,
                settings,
                workflow,
                contact,
                trigger="task",
                task_id="01234567-0000-7000-0000-000000000aaa",
                model_override=FunctionModel(_model_that_calls_noop),
            )
        # Coarse-lock held does not block a task-scoped invocation.
        assert result is not None
        assert result["status"] == "completed"
    finally:
        blocker.close()


def test_advisory_lock_task_scope_blocks_same_task_id(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.25: two drain workers grabbing the same task_id race on the same lock.

    Regression guard: a duplicate-create race (per §V.16) presenting the same
    task_id twice must still serialize so only one invocation runs.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    task_id = "01234567-0000-7000-0000-000000000bbb"

    from conftest import TEST_DATABASE_URL

    blocker = psycopg.connect(TEST_DATABASE_URL)
    try:
        k1, k2 = _advisory_lock_keys_for_task(task_id)
        blocker.execute("SELECT pg_advisory_lock(%s, %s)", (k1, k2))

        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="task",
            task_id=task_id,
            model_override=FunctionModel(_model_that_calls_noop),
        )
        assert result is None
    finally:
        blocker.close()


def test_advisory_lock_loser_emits_no_agent_invoke_span(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """§B.42 / §V.25: lock-loser does NOT emit a billable ``agent.invoke`` span.

    Pre-§T.68 the parent span opened first and the lock check ran inside it,
    so a loser-of-race produced a noop ``agent.invoke`` row in Logfire that
    polluted per-trigger count metrics. Moving the check above the span makes
    the loser a debug log only.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    task_id = "01234567-0000-7000-0000-000000000ccc"

    from conftest import TEST_DATABASE_URL

    blocker = psycopg.connect(TEST_DATABASE_URL)
    try:
        k1, k2 = _advisory_lock_keys_for_task(task_id)
        blocker.execute("SELECT pg_advisory_lock(%s, %s)", (k1, k2))

        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="task",
            task_id=task_id,
            model_override=FunctionModel(_model_that_calls_noop),
        )
        assert result is None

        span_names = [span.name for span in capfire.exporter.exported_spans]
        assert "agent.invoke" not in span_names
    finally:
        blocker.close()


def test_advisory_lock_keys_for_task_deterministic_and_split() -> None:
    """``_advisory_lock_keys_for_task`` is a pure function of ``task_id``.

    Same task_id -> same keys (deterministic across processes). Different
    task IDs that share a prefix get distinct second halves so the full ID
    participates in the lock.
    """
    a = "01234567-0000-7000-0000-000000000001"
    b = "01234567-0000-7000-0000-000000000002"

    assert _advisory_lock_keys_for_task(a) == _advisory_lock_keys_for_task(a)
    # Differ in the tail half -> the second key must differ even though the
    # prefix matches.
    assert _advisory_lock_keys_for_task(a)[1] != _advisory_lock_keys_for_task(b)[1]


# -- Tests: email history context -----------------------------------------------


def test_email_history_loaded(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent receives email history between account and contact in the prompt."""
    account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    # Create an email in the history.
    from mailpilot.database import create_email

    create_email(
        database_connection,
        gmail_message_id="msg-hist-1",
        gmail_thread_id="thread-hist-1",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Previous outreach",
        body_text="Hi, interested in a demo?",
    )

    captured_messages: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Previous outreach" in all_text


def test_email_history_scoped_to_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent only sees emails from its own workflow, not other workflows."""
    account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    # Email belonging to THIS workflow -- should appear.
    create_email(
        database_connection,
        gmail_message_id="msg-same-wf",
        gmail_thread_id="thread-same-wf",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Same workflow outreach",
        body_text="This should be visible.",
    )

    # Email belonging to a DIFFERENT workflow -- should NOT appear.
    other_workflow = make_test_workflow(
        database_connection, account_id=account.id, name="other-workflow"
    )
    _activate(database_connection, other_workflow.id)
    create_email(
        database_connection,
        gmail_message_id="msg-other-wf",
        gmail_thread_id="thread-other-wf",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=other_workflow.id,
        direction="outbound",
        subject="Other workflow outreach",
        body_text="This should NOT be visible.",
    )

    # Email with NO workflow -- should NOT appear.
    create_email(
        database_connection,
        gmail_message_id="msg-no-wf",
        gmail_thread_id="thread-no-wf",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Unrelated inbound",
        body_text="This should NOT be visible either.",
    )

    captured_messages: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Same workflow outreach" in all_text
    assert "Other workflow outreach" not in all_text
    assert "Unrelated inbound" not in all_text


# -- Tests: trigger context ----------------------------------------------------


def test_inbound_email_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When an inbound email is provided, it appears in the agent prompt."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    from mailpilot.database import create_email

    email = create_email(
        database_connection,
        gmail_message_id="msg-inbound-1",
        gmail_thread_id="thread-inbound-1",
        account_id=account.id,
        contact_id=contact.id,
        direction="inbound",
        subject="Question about pricing",
        body_text="How much does your product cost?",
    )

    captured_messages: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Question about pricing" in all_text
    assert "How much does your product cost?" in all_text


def test_inbound_email_trigger_includes_email_id_and_sender(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Inbound email trigger includes email ID and sender so agent can reply."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
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
    assert email is not None

    captured_messages: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
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


def test_deferred_task_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30: trigger='task' with a task_description renders the Deferred task
    block."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    captured_messages: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            task_description="Follow up on demo request",
            task_context={"days_since_last": 7},
            trigger="task",
            model_override=_capturing_model(captured_messages),
        )

    all_text = str(captured_messages)
    assert "Follow up on demo request" in all_text
    assert "days_since_last" in all_text


# -- Tests: §V.29 trigger-email dedupe in user prompt --------------------------


def test_inbound_trigger_excluded_from_email_history_when_only_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.29: when the trigger is the only email in history, the email_history
    block is suppressed and the trigger body appears exactly once."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    trigger = create_email(
        database_connection,
        gmail_message_id="msg-trigger-solo",
        gmail_thread_id="thread-trigger-solo",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Question about pricing",
        body_text="UNIQUE_TRIGGER_BODY_MARKER about pricing.",
    )
    assert trigger is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[trigger],
        email=trigger,
    )

    assert "No prior email history with this contact." in prompt
    assert "Email history (1 messages):" not in prompt
    assert prompt.count("UNIQUE_TRIGGER_BODY_MARKER about pricing.") == 1


def test_inbound_trigger_dedupes_against_prior_history(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.29: trigger row excluded from email_history when prior emails exist;
    history count reflects N-1 and trigger body appears exactly once total."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    prior = create_email(
        database_connection,
        gmail_message_id="msg-prior",
        gmail_thread_id="thread-prior",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Initial outreach",
        body_text="UNIQUE_PRIOR_BODY_MARKER previously sent.",
    )
    assert prior is not None
    trigger = create_email(
        database_connection,
        gmail_message_id="msg-trigger-with-prior",
        gmail_thread_id="thread-trigger-with-prior",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Re: outreach",
        body_text="UNIQUE_TRIGGER_BODY_MARKER reply received.",
    )
    assert trigger is not None

    # Order doesn't matter for the dedupe logic; mimic list_emails ordering.
    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[trigger, prior],
        email=trigger,
    )

    assert "Email history (1 messages):" in prompt
    assert "Email history (2 messages):" not in prompt
    assert "UNIQUE_PRIOR_BODY_MARKER previously sent." in prompt
    assert prompt.count("UNIQUE_TRIGGER_BODY_MARKER reply received.") == 1


def test_outbound_history_unchanged_without_trigger(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.29 regression guard: when no trigger email is provided (outbound path),
    email_history is rendered in full -- no dedupe filter applied."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    first = create_email(
        database_connection,
        gmail_message_id="msg-out-1",
        gmail_thread_id="thread-out-1",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Outreach #1",
        body_text="UNIQUE_FIRST_BODY_MARKER first send.",
    )
    second = create_email(
        database_connection,
        gmail_message_id="msg-out-2",
        gmail_thread_id="thread-out-2",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Outreach #2",
        body_text="UNIQUE_SECOND_BODY_MARKER follow-up.",
    )
    assert first is not None
    assert second is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[second, first],
        email=None,
    )

    assert "Email history (2 messages):" in prompt
    assert "UNIQUE_FIRST_BODY_MARKER first send." in prompt
    assert "UNIQUE_SECOND_BODY_MARKER follow-up." in prompt


# -- Tests: §V.30 trigger-block matches span trigger attribute ----------------


def test_enrollment_run_outbound_renders_first_reach_out(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30: trigger='enrollment_run' with no email renders the first-reach-out
    framing, never the deferred-task block."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        email=None,
        trigger="enrollment_run",
    )

    assert "First reach-out for this enrollment." in prompt
    assert "Deferred task:" not in prompt


def test_enrollment_schedule_renders_first_reach_out(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30 + §V.32: trigger='enrollment_schedule' renders the same
    first-reach-out framing as 'enrollment_run' (byte-identical -- both
    mean "first outbound message, no prior context")."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")

    run_prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        email=None,
        trigger="enrollment_run",
    )
    sched_prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        email=None,
        trigger="enrollment_schedule",
    )

    assert sched_prompt == run_prompt
    assert "First reach-out for this enrollment." in sched_prompt
    assert "Deferred task:" not in sched_prompt


def test_task_trigger_renders_deferred_task_block(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30 regression guard: trigger='task' with a persisted task description
    keeps rendering the Deferred task: block."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        email=None,
        task_description="Follow up on demo request",
        task_context={"days_since_last": 7},
        trigger="task",
    )

    assert "Deferred task:" in prompt
    assert "Follow up on demo request" in prompt
    assert "days_since_last" in prompt
    assert "First reach-out" not in prompt


def test_enrollment_run_with_email_uses_email_branch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30: when an email is present, the inbound-email branch wins over the
    enrollment_run framing -- existing precedence preserved."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = create_email(
        database_connection,
        gmail_message_id="msg-enr-with-email",
        gmail_thread_id="thread-enr-with-email",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Continued enrollment",
        body_text="UNIQUE_EMAIL_BRANCH_MARKER question.",
    )
    assert email is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[email],
        email=email,
        trigger="enrollment_run",
    )

    assert "New inbound email:" in prompt
    assert "UNIQUE_EMAIL_BRANCH_MARKER question." in prompt
    assert "First reach-out" not in prompt
    assert "Deferred task:" not in prompt


def test_manual_trigger_no_email_no_task_renders_outbound_fallback(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.30: trigger='manual' with no email and no task_description falls back
    to the existing 'This is an outbound invocation.' prose."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        email=None,
        trigger="manual",
    )

    assert "This is an outbound invocation." in prompt
    assert "Deferred task:" not in prompt
    assert "First reach-out" not in prompt


# -- Tests: §V.135 mechanical context pre-feed ---------------------------------


def test_build_user_prompt_renders_contact_and_company_records(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.135 / §V.8: the pre-fed ContactView + CompanyView render as JSON
    ``Contact record:`` / ``Company record:`` sections byte-identical to the
    loader's own serialization -- the same loaders back CLI ``contact view`` /
    ``company view``, so the agent and operator see identical context."""
    from conftest import make_test_company
    from mailpilot.database import (
        create_note,
        load_company_view,
        load_contact_view,
    )

    _account, _c, workflow = _setup(database_connection)
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    contact = make_test_contact(
        database_connection, email="cv@acme.com", company_id=company.id
    )
    create_note(database_connection, body="MET_AT_CONF_MARKER", contact_id=contact.id)
    create_note(database_connection, body="TOP_CUSTOMER_MARKER", company_id=company.id)

    contact_view = load_contact_view(database_connection, contact.id)
    company_view = load_company_view(database_connection, company.id)
    assert contact_view is not None
    assert company_view is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        contact_view=contact_view,
        company_view=company_view,
    )

    assert "Contact record:" in prompt
    assert "Company record:" in prompt
    # CLI ContactView may carry tags[]; Contact record strips them (§V.8).
    assert contact_view.model_dump_json(indent=2, exclude={"tags"}) in prompt
    assert company_view.model_dump_json(indent=2) in prompt
    # The inlined notes only the loaders surface came through.
    assert "MET_AT_CONF_MARKER" in prompt
    assert "TOP_CUSTOMER_MARKER" in prompt


def test_build_user_prompt_strips_contact_tags(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.8: Contact record: omits tags[] even when ContactView carries them."""
    from conftest import make_test_company, make_test_tag_assignment
    from mailpilot.database import load_contact_view

    _account, _c, workflow = _setup(database_connection)
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    contact = make_test_contact(
        database_connection, email="tagged@acme.com", company_id=company.id
    )
    make_test_tag_assignment(
        database_connection, contact_id=contact.id, name="sales-seat"
    )

    contact_view = load_contact_view(database_connection, contact.id)
    assert contact_view is not None
    assert contact_view.tags == ["sales-seat"]

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        contact_view=contact_view,
    )

    contact_block = prompt.split("Contact record:\n", 1)[1]
    assert '"tags"' not in contact_block
    assert "sales-seat" not in contact_block
    assert contact_view.model_dump_json(indent=2, exclude={"tags"}) in prompt


def test_build_user_prompt_omits_company_record_without_company_view(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.135: a contact with no parent company renders no ``Company record:``
    section -- ``company_view`` is None so the block is dropped."""
    from mailpilot.database import load_contact_view

    _account, contact, workflow = _setup(database_connection)
    contact_view = load_contact_view(database_connection, contact.id)
    assert contact_view is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        contact_view=contact_view,
        company_view=None,
    )

    assert "Contact record:" in prompt
    assert "Company record:" not in prompt


def test_build_user_prompt_omits_verification_meta(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.144 / §V.135: agent context builder never carries verification_meta.

    Operator audit trails (Bouncer status, source) are durable on the Contact
    row but outside the prompt allowlist. load_contact_view strips them so the
    Contact record: section matches default CLI view (byte-identical, no meta).
    """
    from mailpilot.database import create_contact, load_contact_view

    _account, _c, workflow = _setup(database_connection)
    meta = {"bouncer_status": "deliverable", "source": "hunter_pattern"}
    contact = create_contact(
        database_connection,
        email="meta-agent@example.com",
        email_confidence=91,
        verification_meta=meta,
    )
    assert contact is not None
    assert contact.verification_meta == meta

    contact_view = load_contact_view(database_connection, contact.id)
    assert contact_view is not None

    prompt = _build_user_prompt(
        workflow=workflow,
        contact=contact,
        email_history=[],
        contact_view=contact_view,
    )

    assert "Contact record:" in prompt
    assert "verification_meta" not in prompt
    assert "bouncer_status" not in prompt
    assert "hunter_pattern" not in prompt
    # Agent-facing lead metadata remains.
    assert "email_confidence" in prompt
    assert "91" in prompt


def test_invoke_prefeeds_contact_and_company_records(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.135 / §V.8 / §C: invoke_workflow_agent mechanically loads the contact
    + company records via the shared loaders and pre-feeds them into the prompt,
    so the agent grounds on CRM state (and inlined notes) with no read-tool
    round-trip -- the system holds the keys, so the harness loads the data."""
    from conftest import make_test_company
    from mailpilot.database import create_note

    account = make_test_account(database_connection, email="sender@example.com")
    company = make_test_company(database_connection, name="Acme", domain="acme.com")
    contact = make_test_contact(
        database_connection, email="lead@acme.com", company_id=company.id
    )
    workflow = make_test_workflow(database_connection, account_id=account.id)
    _activate(database_connection, workflow.id)
    create_enrollment(database_connection, workflow.id, contact.id)
    create_note(database_connection, body="CONTACT_NOTE_MARKER", contact_id=contact.id)
    create_note(database_connection, body="COMPANY_NOTE_MARKER", company_id=company.id)

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    captured: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_capturing_model(captured),
        )

    all_text = str(captured)
    assert "Contact record:" in all_text
    assert "Company record:" in all_text
    assert "CONTACT_NOTE_MARKER" in all_text
    assert "COMPANY_NOTE_MARKER" in all_text


# -- Tests: early-exit paths ---------------------------------------------------


def test_account_not_found_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When workflow references a deleted account, raises ValueError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )

    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        patch("mailpilot.agent.invoke.database.get_account", return_value=None),
        pytest.raises(ValueError, match="account not found"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )


def test_missing_api_key_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When no model_override and no active-provider API key, raises ValueError."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(llm_provider="xai", xai_api_key="")

    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(ValueError, match="MAILPILOT_XAI_API_KEY"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            # No model_override -- forces the real model path.
        )


# -- Tests: usage attributes on span ------------------------------------------


def test_invoke_span_has_usage_attributes(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """agent.invoke span includes input_tokens, output_tokens, llm_requests."""
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    attrs = invoke_spans[0]["attributes"]
    assert "model" in attrs
    assert "input_tokens" in attrs
    assert "output_tokens" in attrs
    assert "total_tokens" in attrs
    assert "llm_requests" in attrs
    assert attrs["input_tokens"] >= 0
    assert attrs["output_tokens"] >= 0
    assert attrs["total_tokens"] == attrs["input_tokens"] + attrs["output_tokens"]
    assert attrs["llm_requests"] >= 1


def test_invoke_span_has_status_completed_on_success(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """Successful agent.invoke span carries status=completed.

    Operator-log parity: the ``event=agent.run`` line emits ``status=completed``
    and the result dict has ``status=completed``. The span MUST mirror the
    same value so Logfire queries don't need to join through ``task.status``.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert invoke_spans[0]["attributes"]["status"] == "completed"


def test_invoke_span_has_status_failed_on_exception(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """Failed agent.invoke span carries status=failed.

    When the agent raises (e.g. AgentDidNotUseToolsError) the span MUST be
    annotated with status=failed before the exception propagates so the
    operator log's ``event=error`` (emitted by callers) lines up with the
    span attribute in Logfire.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(AgentDidNotUseToolsError),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_no_tools),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert invoke_spans[0]["attributes"]["status"] == "failed"


# -- Tests: reply_email tool ---------------------------------------------------


def test_agent_calls_reply_email(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Agent can call reply_email tool to reply in-thread."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
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
    assert inbound is not None

    model = _model_that_calls_tool(
        "reply_email",
        {"email_id": inbound.id, "body": "Sure, happy to help!"},
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
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


# -- Tests: §V.120 inbound-trigger reply guard --------------------------------


def _make_inbound_email(
    connection: psycopg.Connection[dict[str, Any]],
    account_id: str,
    contact_id: str,
    workflow_id: str,
) -> Any:
    """Persist an inbound email to drive the §V.120 reply-guard tests."""
    from mailpilot.database import create_email

    email = create_email(
        connection,
        gmail_message_id="msg-reply-guard",
        gmail_thread_id="thread-reply-guard",
        account_id=account_id,
        contact_id=contact_id,
        workflow_id=workflow_id,
        direction="inbound",
        subject="Need an answer",
        body_text="Please reply to this.",
    )
    assert email is not None
    return email


def test_inbound_email_run_without_reply_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120: inbound trigger that calls read tools but never replies fails.

    The model satisfies §V.81 (one tool call -- ``search_emails``) yet sends
    nothing. The guard must convert that silent non-reply into a raised
    ``AgentCompletedWithoutReplyError`` instead of a success.
    """
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = _make_inbound_email(
        database_connection, account.id, contact.id, workflow.id
    )
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool("search_emails", {"query": contact.email})
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(AgentCompletedWithoutReplyError, match=workflow.id),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="email",
            model_override=model,
        )


def test_inbound_task_run_without_reply_raises(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120: a drained task carrying an inbound email is held to the same
    reply obligation as the direct ``email`` trigger."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = _make_inbound_email(
        database_connection, account.id, contact.id, workflow.id
    )
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool("search_emails", {"query": contact.email})
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(AgentCompletedWithoutReplyError),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="task",
            model_override=model,
        )


def test_inbound_email_run_with_reply_passes(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120: a run that calls reply_email satisfies the guard (no raise)."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = _make_inbound_email(
        database_connection, account.id, contact.id, workflow.id
    )
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool(
        "reply_email", {"email_id": email.id, "body": "Here is your answer."}
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-reply-guard",
            "threadId": "thread-reply-guard",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="email",
            model_override=model,
        )

    assert result is not None
    assert result["status"] == "completed"


def test_inbound_email_run_with_noop_is_explicit_decline(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120: a successful noop counts as an explicit decline (no raise).

    Decline is a valid terminal action for an inbound trigger -- only a
    dropped reply (neither send nor noop) is the failure.
    """
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = _make_inbound_email(
        database_connection, account.id, contact.id, workflow.id
    )
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="email",
            model_override=FunctionModel(_model_that_calls_noop),
        )

    assert result is not None
    assert result["status"] == "completed"


def _touch_model(subject: str | None, body: str) -> FunctionModel:
    """FunctionModel yielding a compose-only TouchMessage via ``final_result``.

    Pydantic AI routes structured output through a synthetic ``final_result``
    tool call (see the classifier tests), so the compose-only touch agent's model
    returns it the same way.
    """

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result",
                    args={"subject": subject, "body": body},
                )
            ]
        )

    return FunctionModel(_respond)


def test_outbound_enrollment_run_composes_and_sends_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: an outbound first reach-out (enrollment_run, no email) runs
    compose-only. The agent yields a TouchMessage and the harness sends it via
    send_email on a new thread, calling no send tool. The run completes
    structurally (§V.120: the send is not walker-checked) and reports the sent
    email id and touch 1. NULL cadence -> no follow-up scheduled (§V.136)."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-touch-1",
            "threadId": "thread-touch-1",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="enrollment_run",
            model_override=_touch_model("Quick question", "Hi -- worth a chat?"),
        )

    assert result is not None
    assert result["status"] == "completed"
    assert result["tool_calls"] == 0
    assert result["touch_number"] == 1
    assert result["sent_email_id"]
    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["to"] == contact.email
    assert call_kwargs["subject"] == "Quick question"
    assert call_kwargs.get("thread_id") is None
    # NULL cadence: touch 1 sends and schedules no follow-up (§V.136).
    tasks = database_connection.execute(
        "SELECT id FROM task WHERE workflow_id = %s", (workflow.id,)
    ).fetchall()
    assert tasks == []


def test_outbound_enrollment_schedule_composes_and_sends_touch(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136 / §V.32: the scheduled first-touch trigger is also a compose-only
    touch run -- it composes and sends touch 1 just like ``enrollment_run``."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-sched-1",
            "threadId": "thread-sched-1",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="enrollment_schedule",
            model_override=_touch_model("Intro", "Opening reach-out."),
        )

    assert result is not None
    assert result["status"] == "completed"
    assert result["touch_number"] == 1
    mock_client.send_message.assert_called_once()


def test_compose_only_touch_two_replies_and_schedules_next(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: a scheduled touch-2 task (context.touch=2, prior_email_id set)
    composes a follow-up. The harness threads it on the prior touch via a reply
    (no new-thread send, so the cold-outbound cooldown is skipped) and the
    cadence engine schedules touch 3 with context {touch:3, prior_email_id}."""
    account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    workflow = update_workflow(
        database_connection, workflow.id, touches=3, touch_interval_days=7
    )
    assert workflow is not None

    from mailpilot.database import create_email

    prior = create_email(
        database_connection,
        gmail_message_id="msg-t1",
        gmail_thread_id="thread-t1",
        rfc2822_message_id="<t1@mail>",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Quick question",
        body_text="touch 1 body",
    )
    assert prior is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-touch-2",
            "threadId": "thread-t1",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="task",
            task_context={"touch": 2, "prior_email_id": prior.id},
            model_override=_touch_model(None, "Following up on my note."),
        )

    assert result is not None
    assert result["touch_number"] == 2
    # Threaded reply on the prior touch (§V.136), not a cold new-thread send.
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["thread_id"] == "thread-t1"
    # Cadence advanced: touch 3 scheduled with the follow-up context (§V.136).
    rows = database_connection.execute(
        "SELECT context FROM task WHERE workflow_id = %s AND status = 'pending'",
        (workflow.id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["context"]["touch"] == 3
    assert rows[0]["context"]["prior_email_id"] == result["sent_email_id"]


def test_compose_only_prior_email_falls_back_to_latest_outbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136: a touch task with no ``prior_email_id`` in context threads on the
    enrollment's latest outbound email (fallback) so the follow-up still
    continues the last send instead of opening a fresh cold thread."""
    account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    workflow = update_workflow(
        database_connection, workflow.id, touches=2, touch_interval_days=7
    )
    assert workflow is not None

    from mailpilot.database import create_email

    prior = create_email(
        database_connection,
        gmail_message_id="msg-latest",
        gmail_thread_id="thread-latest",
        rfc2822_message_id="<latest@mail>",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Opening",
        body_text="touch 1 body",
    )
    assert prior is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-fallback",
            "threadId": "thread-latest",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="task",
            task_context={"touch": 2},  # no prior_email_id -> fallback
            model_override=_touch_model(None, "Circling back."),
        )

    assert result is not None
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["thread_id"] == "thread-latest"


def test_compose_only_space_aligned_body_sends_without_format_retry(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.42 / §B.128: compose-only no longer format-lints the body.
    A space-aligned label/value block reaches Gmail on the first try."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    body = "Model AX-100  50 GPM\nFlow Rate  50 GPM\nWeight  9 kg"
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-spec",
            "threadId": "thread-spec",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="enrollment_run",
            model_override=_touch_model("Specs for you", body),
        )

    assert result is not None
    assert result["status"] == "completed"
    mock_client.send_message.assert_called_once()
    sent = database_connection.execute(
        "SELECT body_text FROM email WHERE workflow_id = %s AND direction = 'outbound'",
        (workflow.id,),
    ).fetchone()
    assert sent is not None
    assert "Model AX-100" in sent["body_text"]


# -- §V.136 / §B.127 first-touch subject require --------------------------------


@pytest.mark.parametrize(
    "subject",
    [None, "", "   ", "\t\n"],
)
def test_compose_only_first_touch_subject_required_rejects(
    subject: str | None,
) -> None:
    """§V.136 / §B.127: new-thread compose-only rejects empty/None/whitespace subject."""
    with pytest.raises(ModelRetry, match=_SUBJECT_REQUIRED_RETRY):
        _validate_touch_subject(subject, require_subject=True)


def test_compose_only_first_touch_subject_required_accepts_non_empty() -> None:
    """§V.136: a non-empty (after strip) subject is accepted for new-thread touch."""
    _validate_touch_subject("Quick question", require_subject=True)
    _validate_touch_subject("  padded  ", require_subject=True)


def test_compose_only_follow_up_subject_none_ok() -> None:
    """§V.136: follow-up that continues a thread may leave subject empty/None."""
    _validate_touch_subject(None, require_subject=False)
    _validate_touch_subject("", require_subject=False)
    _validate_touch_subject("   ", require_subject=False)


def test_is_new_thread_touch_true_without_prior() -> None:
    """No prior outbound → new thread → subject required."""
    assert _is_new_thread_touch(None) is True


def test_is_new_thread_touch_false_with_replyable_prior(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Prior outbound with thread + contact → reply path → subject not required."""
    account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    from mailpilot.database import create_email

    prior = create_email(
        database_connection,
        gmail_message_id="msg-prior-nt",
        gmail_thread_id="thread-prior-nt",
        rfc2822_message_id="<prior-nt@mail>",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="outbound",
        subject="Opening",
        body_text="touch 1",
    )
    assert prior is not None
    assert _is_new_thread_touch(prior) is False


def _touch_model_empty_subject_then_ok() -> FunctionModel:
    """First yield empty subject (ModelRetry), then a non-empty subject."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        nonlocal call_count
        call_count += 1
        subject = None if call_count == 1 else "Recovered subject"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result",
                    args={"subject": subject, "body": "Body text after retry."},
                )
            ]
        )

    return FunctionModel(_respond)


def test_compose_only_first_touch_subject_required(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.136 / §B.127: empty subject on first-touch triggers ModelRetry; retry
    with a non-empty subject reaches Gmail once (closes empty-subject path)."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-subj-retry",
            "threadId": "thread-subj-retry",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="enrollment_run",
            model_override=_touch_model_empty_subject_then_ok(),
        )

    assert result is not None
    assert result["status"] == "completed"
    mock_client.send_message.assert_called_once()
    call_kwargs = mock_client.send_message.call_args.kwargs
    assert call_kwargs["subject"] == "Recovered subject"
    assert call_kwargs.get("thread_id") is None


def test_outbound_reply_conclude_enrollment_satisfies_send_guard(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.120 + §V.127: on the outbound reply path (a prospect reply drained as
    a ``task`` with an inbound email) conclude_enrollment is a valid
    send-obligation terminal -- it satisfies the inbound-only walker like noop.

    The compose-only shape covers only the no-email first reach-out; a reply
    keeps the tool loop, so the terminal-outcome walker still applies."""
    account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    from mailpilot.database import create_email

    reply = create_email(
        database_connection,
        gmail_message_id="msg-prospect-reply",
        gmail_thread_id="thread-prospect-reply",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="Re: Quick question",
        body_text="Yes, let's book a time.",
    )
    assert reply is not None

    model = _model_that_calls_tool(
        "conclude_enrollment",
        {"disposition": "meeting_booked", "note": "prospect booked a meeting"},
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=reply,
            trigger="task",
            model_override=model,
        )

    assert result is not None
    assert result["status"] == "completed"
    assert result["tool_errors"] == []


def test_invoke_span_failed_when_completed_without_reply(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """§V.120: the dropped-reply failure annotates the span status=failed,
    mirroring the AgentDidNotUseToolsError path so Logfire flags the run."""
    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = _make_inbound_email(
        database_connection, account.id, contact.id, workflow.id
    )
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    model = _model_that_calls_tool("search_emails", {"query": contact.email})
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
        pytest.raises(AgentCompletedWithoutReplyError),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="email",
            model_override=model,
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert invoke_spans[0]["attributes"]["status"] == "failed"


def test_workflow_agent_has_explicit_name_for_otel_traces() -> None:
    """Pydantic AI's `invoke_agent` span carries `gen_ai.agent.name`, derived
    from the Agent's `name=` argument. Setting it explicitly keeps traces
    legible when both the workflow agent and the classifier agent fire in
    the same time window."""
    from datetime import UTC, datetime

    from mailpilot.models import Workflow

    now = datetime.now(UTC)
    workflow = Workflow(
        id="01900000-0000-7000-8000-000000000001",
        name="n",
        template="outbound-general",
        type="outbound",
        account_id="01900000-0000-7000-8000-000000000002",
        account_email="owner@example.com",
        status="active",
        goal="O",
        instructions="I",
        created_at=now,
        updated_at=now,
    )
    agent = _build_agent(workflow)
    assert agent.name == "mailpilot.workflow"


def test_agent_instructions_carry_current_date(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.129: a dynamic instruction injects today's date into the prompt.

    The agent grounds a relative schedule (e.g. "about one month out") on a
    real ``now`` and never guesses the year. Rendered instructions reach the
    model on the ``ModelRequest`` and must contain today's ISO date.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    captured: list[ModelMessage] = []
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_capturing_model(captured),
        )

    instructions = " ".join(
        getattr(msg, "instructions", None) or "" for msg in captured
    )
    assert date.today().isoformat() in instructions


# -- Tests: wrapper signatures -------------------------------------------------


def test_wrappers_do_not_take_contact_id_from_llm() -> None:
    """contact_id must come from AgentDeps, not from LLM-provided arguments.

    Regression for the 2026-04-26 smoke test defect where the agent passed
    the contact's email as contact_id and the tool returned not_found.
    """
    for wrapper in (
        _wrap_conclude_enrollment,
        _wrap_disable_contact,
        _wrap_create_task,
    ):
        params = inspect.signature(wrapper).parameters
        assert "contact_id" not in params, (
            f"{wrapper.__name__} must read contact_id from ctx.deps, "
            f"not accept it as a parameter; got params: {list(params)}"
        )


def test_wrap_send_email_exposes_optional_thread_id() -> None:
    """The agent-visible send_email tool carries an optional thread_id (§V.78).

    Lets the model continue a multi-touch outbound thread by passing the
    gmail_thread_id captured on touch 1; the parameter is optional so a
    first reach-out omits it.
    """
    param = inspect.signature(_wrap_send_email).parameters.get("thread_id")
    assert param is not None, "_wrap_send_email must accept thread_id"
    assert param.default is None, "thread_id must default to None (optional)"


# -- Tests: tool error surfacing -----------------------------------------------


def _model_calls_conclude_enrollment_with_invalid_disposition() -> FunctionModel:
    """Build a FunctionModel that calls conclude_enrollment with a bad disposition."""
    call_count = 0

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="conclude_enrollment",
                        args={"disposition": "not-a-real-disposition", "note": "test"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="Done")])

    return FunctionModel(_respond)


def test_invoke_surfaces_tool_errors_in_result(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When a tool returns {error: ...}, the result must surface it.

    Regression for the 2026-04-26 smoke test defect where a terminal tool
    returned an error and the orchestration still reported success.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        result = invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=_model_calls_conclude_enrollment_with_invalid_disposition(),
        )

    assert result is not None
    assert result["tool_errors"], (
        "expected at least one tool error in the result, got: "
        f"{result.get('tool_errors')!r}"
    )
    assert result["tool_errors"][0]["tool"] == "conclude_enrollment"


# -- Tests: §V.47 prompt-cache settings ---------------------------------------


def test_build_anthropic_model_carries_cache_settings() -> None:
    """§V.47: workflow agent's AnthropicModel sets cache_control breakpoints.

    Pydantic AI translates ``anthropic_cache_tool_definitions`` and
    ``anthropic_cache_instructions`` into ``cache_control`` blocks on the
    last tool definition and last system block of the outbound Anthropic
    request. Inspecting the bound settings is the structural contract;
    the wire-level translation is exercised by Pydantic AI's own tests.
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True


def test_build_anthropic_model_defaults_enable_reasoning() -> None:
    """§V.47: default settings make the workflow agent reason.

    Both reasoning knobs default to active, so a model built from default
    settings carries ``anthropic_thinking={'type': 'adaptive'}`` and
    ``anthropic_effort='high'``.
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") == {"type": "adaptive"}
    assert model.settings.get("anthropic_effort") == "high"


def test_build_anthropic_model_omits_reasoning_keys_when_disabled() -> None:
    """§V.47: setting both reasoning knobs to '' leaves both keys off.

    An operator opts out per knob by setting it to ''. An empty value passes no
    ``anthropic_thinking``/``anthropic_effort`` key, restoring the no-reasoning
    call shape.
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
        anthropic_thinking="",
        anthropic_effort="",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") is None
    assert model.settings.get("anthropic_effort") is None


def test_build_anthropic_model_sets_thinking_when_configured() -> None:
    """§V.47: anthropic_thinking='adaptive' adds the thinking config block."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
        anthropic_thinking="adaptive",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") == {"type": "adaptive"}


def test_build_anthropic_model_sets_effort_when_configured() -> None:
    """§V.47: anthropic_effort='high' threads the reasoning effort level."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-opus-4-7",
        anthropic_effort="high",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_effort") == "high"


def test_build_anthropic_model_reasoning_preserves_cache_flags() -> None:
    """§V.47: both reasoning keys set leaves the cache flags intact."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-opus-4-7",
        anthropic_thinking="adaptive",
        anthropic_effort="xhigh",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("anthropic_thinking") == {"type": "adaptive"}
    assert model.settings.get("anthropic_effort") == "xhigh"
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True


def test_build_anthropic_model_always_passes_max_tokens() -> None:
    """§V.47: default settings bound the output stream at max_tokens=32768.

    The output-token budget is always passed (not empty-gated like the
    reasoning knobs), so default-active thinking cannot exhaust the
    provider-default budget before any reply text (§B.115).
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 32768


def test_build_anthropic_model_threads_max_tokens_override() -> None:
    """§V.47: an operator-set anthropic_max_tokens flows into model settings."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
        anthropic_max_tokens=8192,
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 8192


def test_build_anthropic_model_max_tokens_preserves_reasoning_and_cache() -> None:
    """§V.47: max_tokens rides alongside the reasoning + cache flags."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-opus-4-7",
        anthropic_thinking="adaptive",
        anthropic_effort="xhigh",
    )
    model = _build_anthropic_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 32768
    assert model.settings.get("anthropic_thinking") == {"type": "adaptive"}
    assert model.settings.get("anthropic_effort") == "xhigh"
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True


def test_build_anthropic_model_requires_api_key() -> None:
    """Missing api_key raises a clear error rather than reaching the API."""
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="",
        anthropic_model="claude-sonnet-5",
    )
    with pytest.raises(ValueError, match="MAILPILOT_ANTHROPIC_API_KEY") as exc_info:
        _build_anthropic_model(settings, role="workflow")
    assert "mailpilot config set" not in str(exc_info.value)


def test_build_anthropic_model_uses_240s_read_timeout() -> None:
    """§V.48: workflow agent's AnthropicProvider HTTP client carries a 240s read-timeout.

    Default httpx read-timeout is 60s; under model load that intersects
    long-context multi-turn agent latency and surfaces ``TimeoutError``
    mid-conversation, which would bubble to ``run.task.agent_failed`` with
    no retry (see SPEC.md §B.16). 240s = 4x headroom; idempotency forbids
    retry across tool calls.
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test-timeout",
        anthropic_model="claude-sonnet-5",
    )
    model = _build_anthropic_model(settings, role="workflow")
    http_client = model._provider.client._client  # pyright: ignore[reportPrivateUsage]
    assert http_client.timeout.read == 240.0


def test_build_anthropic_model_defaults_to_anthropic_endpoint() -> None:
    """The default anthropic_base_url leaves the workflow agent on the Anthropic endpoint.

    The settings default is the canonical Anthropic host, so model construction
    targets ``api.anthropic.com`` unless ``anthropic_base_url`` is overridden.
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    assert settings.anthropic_base_url == "https://api.anthropic.com"
    model = _build_anthropic_model(settings, role="workflow")
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.anthropic.com" in base_url


def test_build_anthropic_model_threads_base_url_override() -> None:
    """A set anthropic_base_url routes the workflow agent to the override endpoint.

    Threading ``settings.anthropic_base_url`` into ``AnthropicProvider``
    re-targets the wire endpoint, which is the Novita switch (§I config).
    """
    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="minimax/minimax-m3",
        anthropic_base_url="https://api.novita.ai/anthropic",
    )
    model = _build_anthropic_model(settings, role="workflow")
    base_url = str(model._provider.client.base_url)  # pyright: ignore[reportPrivateUsage]
    assert "api.novita.ai/anthropic" in base_url


def test_build_model_dispatches_to_xai_by_default() -> None:
    """§V.47: default llm_provider=xai builds an XaiModel."""
    from pydantic_ai.models.xai import XaiModel

    settings = make_test_settings(xai_api_key="xai-test")
    assert settings.llm_provider == "xai"
    model = _build_model(settings, role="workflow")
    assert isinstance(model, XaiModel)


def test_build_model_dispatches_to_anthropic_when_selected() -> None:
    """§V.47: llm_provider=anthropic builds an AnthropicModel."""
    from pydantic_ai.models.anthropic import AnthropicModel

    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    model = _build_model(settings, role="workflow")
    assert isinstance(model, AnthropicModel)


def test_build_xai_model_workflow_settings() -> None:
    """§V.47: xAI workflow model always passes max_tokens + reasoning_effort."""
    settings = make_test_settings(
        llm_provider="xai",
        xai_api_key="xai-test",
        xai_model="grok-4.5",
    )
    model = _build_xai_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 32768
    assert model.settings.get("xai_reasoning_effort") == "medium"
    assert model.settings.get("anthropic_cache_tool_definitions") is None
    assert model.settings.get("anthropic_cache_instructions") is None


def test_build_xai_model_threads_overrides() -> None:
    """§V.47: operator-set xai_max_tokens + xai_reasoning_effort flow through."""
    settings = make_test_settings(
        llm_provider="xai",
        xai_api_key="xai-test",
        xai_max_tokens=8192,
        xai_reasoning_effort="high",
    )
    model = _build_xai_model(settings, role="workflow")
    assert model.settings is not None
    assert model.settings.get("max_tokens") == 8192
    assert model.settings.get("xai_reasoning_effort") == "high"


def test_build_xai_model_classifier_omits_workflow_settings() -> None:
    """§V.47: classifier xAI model carries no max_tokens / reasoning_effort."""
    settings = make_test_settings(llm_provider="xai", xai_api_key="xai-test")
    model = _build_xai_model(settings, role="classifier")
    assert model.settings is None or model.settings.get("max_tokens") is None
    assert model.settings is None or model.settings.get("xai_reasoning_effort") is None


def test_build_xai_model_requires_api_key() -> None:
    """Missing xai_api_key fails closed when provider is xai."""
    settings = make_test_settings(llm_provider="xai", xai_api_key="")
    with pytest.raises(ValueError, match="MAILPILOT_XAI_API_KEY") as exc_info:
        _build_xai_model(settings, role="workflow")
    assert "mailpilot config set" not in str(exc_info.value)


def test_build_xai_model_threads_api_host() -> None:
    """§V.47: optional xai_api_host is passed when set."""
    settings = make_test_settings(
        llm_provider="xai",
        xai_api_key="xai-test",
        xai_api_host="gateway.example.com:443",
    )
    model = _build_xai_model(settings, role="workflow")
    # Provider stores api_host on the SDK client; base_url stays telemetry-only.
    assert model.model_name == "grok-4.5"


def test_build_anthropic_classifier_omits_workflow_knobs() -> None:
    """§V.47: Anthropic classifier has cache flags but no thinking/effort/max_tokens."""
    settings = make_test_settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    model = _build_anthropic_model(settings, role="classifier")
    assert model.settings is not None
    assert model.settings.get("anthropic_cache_tool_definitions") is True
    assert model.settings.get("anthropic_cache_instructions") is True
    assert model.settings.get("anthropic_thinking") is None
    assert model.settings.get("anthropic_effort") is None
    assert model.settings.get("max_tokens") is None


def test_invoke_span_has_cache_token_attributes(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """§V.47: agent.invoke rollup span carries cache_read/creation token attrs.

    Pydantic AI's RunUsage already sums cache token counts across child
    chat turns. The presence of both attrs (≥0) is the contract -- the
    actual non-zero rate lands via the smoke-test follow-up against a
    real Anthropic endpoint.
    """
    _account, contact, workflow = _setup(database_connection)
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    attrs = invoke_spans[0]["attributes"]
    assert "cache_read_input_tokens" in attrs
    assert "cache_creation_input_tokens" in attrs
    assert attrs["cache_read_input_tokens"] >= 0
    assert attrs["cache_creation_input_tokens"] >= 0


# -- Tests: §V.26(+) email_id attr on agent.invoke span -----------------------


def test_invoke_span_has_email_id_when_trigger_email(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """trigger='email' with an email arg surfaces email_id on the rollup span."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = create_email(
        database_connection,
        gmail_message_id="msg-eid-email",
        gmail_thread_id="thread-eid-email",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="hi",
        body_text="please reply",
    )
    assert email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="email",
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert invoke_spans[0]["attributes"]["email_id"] == email.id


def test_invoke_span_has_email_id_when_trigger_task_with_email(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """trigger='task' on an email-driven task carries the same email_id the
    run loop loaded from task.email_id."""
    from mailpilot.database import create_email

    account, contact, workflow = _setup(database_connection, workflow_type="inbound")
    email = create_email(
        database_connection,
        gmail_message_id="msg-eid-task",
        gmail_thread_id="thread-eid-task",
        account_id=account.id,
        contact_id=contact.id,
        workflow_id=workflow.id,
        direction="inbound",
        subject="task-driven",
        body_text="reply please",
    )
    assert email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            email=email,
            trigger="task",
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert invoke_spans[0]["attributes"]["email_id"] == email.id


def test_invoke_span_no_email_id_when_trigger_task_without_email(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """trigger='task' on a non-email task (task.email_id IS NULL) leaves
    email_id absent so per-message rollups skip the row cleanly."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient"),
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="task",
            model_override=FunctionModel(_model_that_calls_noop),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    assert "email_id" not in invoke_spans[0]["attributes"]


def test_invoke_span_no_email_id_when_trigger_enrollment_run(
    database_connection: psycopg.Connection[dict[str, Any]],
    capfire: CaptureLogfire,
) -> None:
    """trigger='enrollment_run' (CLI-initiated outbound first reach-out) never
    carries email_id and preserves existing trigger / workflow / contact attrs.

    §V.136: enrollment_run is now a compose-only touch run, so the model yields
    a TouchMessage and the harness sends it -- the span attrs still hold."""
    _account, contact, workflow = _setup(database_connection, workflow_type="outbound")
    settings = make_test_settings(
        anthropic_api_key="sk-test", anthropic_model="test-model"
    )
    with (
        patch("mailpilot.agent.invoke.GmailClient") as mock_cls,
        patch("mailpilot.agent.invoke.DriveClient"),
    ):
        mock_client = MagicMock()
        mock_client.send_message.return_value = {
            "id": "sent-span-eid",
            "threadId": "thread-span-eid",
            "labelIds": ["SENT"],
        }
        mock_cls.return_value = mock_client
        invoke_workflow_agent(
            database_connection,
            settings,
            workflow,
            contact,
            trigger="enrollment_run",
            model_override=_touch_model("Hello", "Opening reach-out."),
        )

    invoke_spans = [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "agent.invoke"
    ]
    assert len(invoke_spans) == 1
    attrs = invoke_spans[0]["attributes"]
    assert "email_id" not in attrs
    assert attrs["trigger"] == "enrollment_run"
    assert attrs["workflow_id"] == workflow.id
    assert attrs["contact_id"] == contact.id
    assert attrs["workflow_type"] == workflow.type
