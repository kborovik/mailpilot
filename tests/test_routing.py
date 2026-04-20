"""Tests for the email routing pipeline (ADR-04)."""

from __future__ import annotations

from typing import Any

import psycopg
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent import classify as classify_module
from mailpilot.database import (
    activate_workflow,
    create_email,
    get_email,
    get_workflow_contact,
    update_workflow,
)
from mailpilot.routing import (
    _is_bounce,  # pyright: ignore[reportPrivateUsage]
    route_email,
)

# -- Helpers -------------------------------------------------------------------


def _activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        objective="Handle inbound inquiries",
        instructions="Reply helpfully",
    )
    activate_workflow(connection, workflow_id)


def _function_model_returning(
    workflow_id: str | None,
    reasoning: str = "",
) -> FunctionModel:
    """Build a FunctionModel that yields a fixed classification result."""

    def _respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="final_result",
                    args={"workflow_id": workflow_id, "reasoning": reasoning},
                ),
            ],
        )

    return FunctionModel(_respond)


# -- Bounce detection (_is_bounce) ---------------------------------------------


def test_is_bounce_detects_mailer_daemon_sender() -> None:
    assert _is_bounce("mailer-daemon@gmail.com", []) is True


def test_is_bounce_detects_postmaster_sender() -> None:
    assert _is_bounce("postmaster@example.com", []) is True


def test_is_bounce_case_insensitive_sender() -> None:
    assert _is_bounce("MAILER-DAEMON@gmail.com", []) is True
    assert _is_bounce("Postmaster@example.com", []) is True


def test_is_bounce_detects_bounce_label() -> None:
    assert _is_bounce("noreply@example.com", ["CATEGORY_BOUNCED"]) is True
    assert _is_bounce("noreply@example.com", ["INBOX", "bounce-notification"]) is True


def test_is_bounce_returns_false_for_normal_email() -> None:
    assert _is_bounce("alice@example.com", ["INBOX"]) is False
    assert _is_bounce("alice@example.com", []) is False


# -- Idempotency ---------------------------------------------------------------


def test_route_email_skips_already_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="idem@example.com")
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="already routed",
        gmail_thread_id="t-idem",
        is_routed=True,
    )
    assert email is not None

    result = route_email(
        database_connection, email, "alice@example.com", make_test_settings()
    )

    assert result.is_routed is True
    assert result.workflow_id is None


# -- Thread match ---------------------------------------------------------------


def test_route_email_thread_match_assigns_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="route@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="thread-xyz",
        workflow_id=workflow.id,
        is_routed=True,
    )
    assert prior is not None

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="thread-xyz",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id == workflow.id
    assert routed.is_routed is True


def test_route_email_thread_match_uses_most_recent_workflow(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="recent@example.com")
    wf_old = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="Old Workflow",
        workflow_type="inbound",
    )
    wf_new = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="New Workflow",
        workflow_type="inbound",
    )

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="first",
        gmail_thread_id="thread-multi",
        workflow_id=wf_old.id,
        is_routed=True,
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="second",
        gmail_thread_id="thread-multi",
        workflow_id=wf_new.id,
        is_routed=True,
    )

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="thread-multi",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id == wf_new.id


def test_route_email_no_gmail_thread_id_goes_to_classification(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """An email without a thread ID skips thread match, goes to classification."""
    account = make_test_account(database_connection, email="nothreadid@example.com")
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="no thread",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    # No active workflows -> unrouted.
    assert routed.is_routed is True
    assert routed.workflow_id is None


# -- LLM classification --------------------------------------------------------


def test_route_email_classifies_when_no_thread_match(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="classify@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Pricing question",
        body_text="How much does your product cost?",
        gmail_thread_id="t-classify",
    )
    assert new_email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-6",
    )
    model = _function_model_returning(
        workflow_id=workflow.id,
        reasoning="pricing inquiry matches inbound workflow",
    )

    with classify_module._AGENT.override(model=model):  # pyright: ignore[reportPrivateUsage]
        routed = route_email(
            database_connection, new_email, "alice@example.com", settings
        )

    assert routed.workflow_id == workflow.id
    assert routed.is_routed is True


def test_route_email_classification_no_match_stores_unrouted(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="unrouted@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Random spam",
        body_text="You won a prize!",
        gmail_thread_id="t-unrouted",
    )
    assert new_email is not None

    settings = make_test_settings(
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-4-6",
    )
    model = _function_model_returning(workflow_id=None, reasoning="no match")

    with classify_module._AGENT.override(model=model):  # pyright: ignore[reportPrivateUsage]
        routed = route_email(
            database_connection, new_email, "alice@example.com", settings
        )

    assert routed.workflow_id is None
    assert routed.is_routed is True


def test_route_email_classification_skips_outbound_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Only inbound workflows are classification candidates."""
    account = make_test_account(database_connection, email="obfilter@example.com")
    outbound_wf = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="Outbound Campaign",
        workflow_type="outbound",
    )
    update_workflow(
        database_connection,
        outbound_wf.id,
        objective="Cold outreach",
        instructions="Send cold emails",
    )
    activate_workflow(database_connection, outbound_wf.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Hi there",
        gmail_thread_id="t-obfilter",
    )
    assert new_email is not None

    # No inbound workflows -> unrouted, LLM never called.
    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id is None
    assert routed.is_routed is True


# -- Unrouted fallback ----------------------------------------------------------


def test_route_email_no_match_sets_routed_true_workflow_null(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """No thread match, no active inbound workflows -> deliberately unrouted."""
    account = make_test_account(database_connection, email="noworkflows@example.com")
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="orphan",
        gmail_thread_id="t-orphan",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    assert routed.is_routed is True
    assert routed.workflow_id is None
    stored = get_email(database_connection, new_email.id)
    assert stored is not None
    assert stored.is_routed is True
    assert stored.workflow_id is None


# -- Bounce detection -----------------------------------------------------------


def test_route_email_bounce_marks_original_outbound_bounced(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="bounce@example.com")
    contact = make_test_contact(
        database_connection, email="recipient@example.com", domain="example.com"
    )

    outbound = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        gmail_thread_id="t-bounce",
        contact_id=contact.id,
        status="sent",
        is_routed=True,
    )
    assert outbound is not None

    bounce_notification = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Delivery Status Notification (Failure)",
        gmail_thread_id="t-bounce",
    )
    assert bounce_notification is not None

    routed = route_email(
        database_connection,
        bounce_notification,
        "mailer-daemon@gmail.com",
        make_test_settings(),
    )

    assert routed.is_routed is True
    # Original outbound email should be marked bounced.
    original = get_email(database_connection, outbound.id)
    assert original is not None
    assert original.status == "bounced"


def test_route_email_bounce_disables_original_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    from mailpilot.database import get_contact

    account = make_test_account(database_connection, email="bdisable@example.com")
    contact = make_test_contact(
        database_connection, email="bounced@example.com", domain="example.com"
    )

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        gmail_thread_id="t-bdisable",
        contact_id=contact.id,
        status="sent",
        is_routed=True,
    )

    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Bounce",
        gmail_thread_id="t-bdisable",
    )
    assert bounce is not None

    route_email(
        database_connection,
        bounce,
        "POSTMASTER@example.com",
        make_test_settings(),
    )

    updated_contact = get_contact(database_connection, contact.id)
    assert updated_contact is not None
    assert updated_contact.status == "bounced"
    assert updated_contact.status_reason != ""


def test_route_email_bounce_via_label(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Bounce detected via Gmail label even if sender is not mailer-daemon."""
    account = make_test_account(database_connection, email="blabel@example.com")
    contact = make_test_contact(
        database_connection, email="labelrecip@example.com", domain="example.com"
    )

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Hello",
        gmail_thread_id="t-blabel",
        contact_id=contact.id,
        status="sent",
        is_routed=True,
    )

    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Bounce",
        gmail_thread_id="t-blabel",
        labels=["INBOX", "CATEGORY_BOUNCED"],
    )
    assert bounce is not None

    routed = route_email(
        database_connection,
        bounce,
        "noreply@google.com",
        make_test_settings(),
    )

    assert routed.is_routed is True
    original = get_email(database_connection, bounce.id)
    assert original is not None
    assert original.is_routed is True


def test_route_email_bounce_no_outbound_in_thread_still_marks_routed(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Bounce notification without a matching outbound is still marked routed."""
    account = make_test_account(database_connection, email="noob@example.com")

    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Bounce",
        gmail_thread_id="t-noob",
    )
    assert bounce is not None

    routed = route_email(
        database_connection,
        bounce,
        "mailer-daemon@gmail.com",
        make_test_settings(),
    )

    assert routed.is_routed is True


# -- workflow_contact creation --------------------------------------------------


def test_route_email_creates_workflow_contact_on_route(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="wcreate@example.com")
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    # Thread match path -> routes to workflow -> should create workflow_contact.
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-wcreate",
        workflow_id=workflow.id,
        is_routed=True,
    )

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="t-wcreate",
        contact_id=contact.id,
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "sender@example.com", make_test_settings()
    )

    wc = get_workflow_contact(database_connection, workflow.id, contact.id)
    assert wc is not None
    assert wc.status == "pending"


def test_route_email_workflow_contact_idempotent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Routing a second email in the same thread doesn't fail on duplicate workflow_contact."""
    account = make_test_account(database_connection, email="wcidem@example.com")
    contact = make_test_contact(
        database_connection, email="repeat@example.com", domain="example.com"
    )
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-wcidem",
        workflow_id=workflow.id,
        is_routed=True,
    )

    # First inbound -> creates workflow_contact.
    email1 = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply 1",
        gmail_thread_id="t-wcidem",
        contact_id=contact.id,
    )
    assert email1 is not None
    route_email(database_connection, email1, "repeat@example.com", make_test_settings())

    # Second inbound -> should NOT raise on duplicate workflow_contact.
    email2 = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply 2",
        gmail_thread_id="t-wcidem",
        gmail_message_id="msg-wcidem-2",
        contact_id=contact.id,
    )
    assert email2 is not None
    routed = route_email(
        database_connection, email2, "repeat@example.com", make_test_settings()
    )

    assert routed.workflow_id == workflow.id
    assert routed.is_routed is True
