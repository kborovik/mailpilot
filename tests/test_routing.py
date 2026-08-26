"""Tests for the email routing pipeline (§V.27)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from logfire.testing import CaptureLogfire
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from conftest import (
    make_test_account,
    make_test_contact,
    make_test_enrollment,
    make_test_settings,
    make_test_workflow,
)
from mailpilot.agent import classify as classify_module
from mailpilot.database import (
    activate_workflow,
    create_email,
    create_task,
    get_contact,
    get_contact_by_email,
    get_email,
    get_enrollment,
    get_latest_enrollment_outcome,
    get_task,
    record_enrollment_outcome,
    update_workflow,
)
from mailpilot.routing import (
    RoutingContext,
    _is_bounce,  # pyright: ignore[reportPrivateUsage]
    find_thread_enrolled_contact,
    mark_routed,
    route_email,
)

_FAR_FUTURE = "2099-12-31T00:00:00Z"


# -- Helpers -------------------------------------------------------------------


def _activate_workflow(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
) -> None:
    """Fill required fields and activate a workflow."""
    update_workflow(
        connection,
        workflow_id,
        goal="Handle inbound inquiries",
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
        name="old-workflow",
        workflow_type="inbound",
    )
    wf_new = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="new-workflow",
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
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
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
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
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
        name="outbound-campaign",
        workflow_type="outbound",
    )
    update_workflow(
        database_connection,
        outbound_wf.id,
        goal="Cold outreach",
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
    contact = make_test_contact(database_connection, email="recipient@example.com")

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
    account = make_test_account(database_connection, email="bdisable@example.com")
    contact = make_test_contact(database_connection, email="bounced@example.com")

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
    assert updated_contact.disabled_reason is not None
    assert updated_contact.disabled_reason.startswith("bounced:")


def test_route_email_bounce_via_label(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Bounce detected via Gmail label even if sender is not mailer-daemon."""
    account = make_test_account(database_connection, email="blabel@example.com")
    contact = make_test_contact(database_connection, email="labelrecip@example.com")

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


def test_route_email_bounce_concludes_outbound_and_cancels_t2(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.163 / §B.133: bounced T1 records do_not_contact + cancels pending T2."""
    account = make_test_account(database_connection, email="bounce-t2@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="bounce-t2@prospect.com")
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    outbound = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-bounce-t2",
        contact_id=contact.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
    )
    assert outbound is not None
    t2 = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="Touch 2 of 3",
        scheduled_at=_FAR_FUTURE,
        context={"touch": 2, "trigger": "followup"},
    )

    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Delivery Status Notification (Failure)",
        gmail_thread_id="t-bounce-t2",
    )
    assert bounce is not None
    route_email(
        database_connection,
        bounce,
        "mailer-daemon@gmail.com",
        make_test_settings(),
    )

    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert get_latest_enrollment_outcome(database_connection, enrollment.id) == "failed"
    outcome_row = database_connection.execute(
        "SELECT detail->>'disposition' AS disposition FROM activity "
        "WHERE enrollment_id = %s AND type = 'enrollment_failed' "
        "ORDER BY created_at DESC LIMIT 1",
        (enrollment.id,),
    ).fetchone()
    assert outcome_row is not None
    assert outcome_row["disposition"] == "do_not_contact"
    still_active = get_enrollment(database_connection, workflow.id, contact.id)
    assert still_active is not None
    assert still_active.status == "active"
    disabled = get_contact(database_connection, contact.id)
    assert disabled is not None
    assert disabled.disabled_reason is not None
    assert disabled.disabled_reason.startswith("bounced:")


def test_route_email_bounce_skips_already_terminal_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.163: bounce does not write a second outcome on an already-terminal enrollment."""
    account = make_test_account(database_connection, email="bounce-term@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="outbound"
    )
    contact = make_test_contact(database_connection, email="bounce-term@prospect.com")
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    record_enrollment_outcome(
        database_connection,
        enrollment.id,
        outcome="completed",
        reason="meeting booked",
        disposition="meeting_booked",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-bounce-term",
        contact_id=contact.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
    )
    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Bounce",
        gmail_thread_id="t-bounce-term",
    )
    assert bounce is not None
    route_email(
        database_connection,
        bounce,
        "mailer-daemon@gmail.com",
        make_test_settings(),
    )

    assert (
        get_latest_enrollment_outcome(database_connection, enrollment.id) == "completed"
    )
    count_row = database_connection.execute(
        "SELECT COUNT(*) AS n FROM activity "
        "WHERE enrollment_id = %s AND type IN "
        "('enrollment_completed', 'enrollment_failed')",
        (enrollment.id,),
    ).fetchone()
    assert count_row is not None
    assert count_row["n"] == 1


def test_route_email_bounce_concludes_every_outbound_skips_inbound(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.163: fan-out every active outbound enrollment; inbound stays open."""
    account = make_test_account(database_connection, email="bounce-fan@example.com")
    outbound_a = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-a",
        workflow_type="outbound",
    )
    outbound_b = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-b",
        workflow_type="outbound",
    )
    inbound = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="inbound-a",
        workflow_type="inbound",
    )
    contact = make_test_contact(database_connection, email="bounce-fan@prospect.com")
    enroll_a = make_test_enrollment(database_connection, outbound_a.id, contact.id)
    enroll_b = make_test_enrollment(database_connection, outbound_b.id, contact.id)
    enroll_in = make_test_enrollment(database_connection, inbound.id, contact.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-bounce-fan",
        contact_id=contact.id,
        workflow_id=outbound_a.id,
        status="sent",
        is_routed=True,
    )
    bounce = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Bounce",
        gmail_thread_id="t-bounce-fan",
    )
    assert bounce is not None
    route_email(
        database_connection,
        bounce,
        "mailer-daemon@gmail.com",
        make_test_settings(),
    )

    assert get_latest_enrollment_outcome(database_connection, enroll_a.id) == "failed"
    assert get_latest_enrollment_outcome(database_connection, enroll_b.id) == "failed"
    assert get_latest_enrollment_outcome(database_connection, enroll_in.id) is None


# -- enrollment creation -------------------------------------------------------


def test_route_email_creates_enrollment_on_route(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    account = make_test_account(database_connection, email="wcreate@example.com")
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    # Thread match path -> routes to workflow -> should create enrollment.
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

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"


def test_route_email_cancels_pending_followups_on_reply(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.123: an inbound reply cancels the enrollment's future follow-up
    touches but preserves the operator first-touch. The enrollment pre-exists
    (the reply case), so the cancel must fire on the ON CONFLICT branch."""
    account = make_test_account(database_connection, email="cancel@example.com")
    contact = make_test_contact(database_connection, email="lead@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-cancel",
        workflow_id=workflow.id,
        is_routed=True,
    )
    enrollment = make_test_enrollment(database_connection, workflow.id, contact.id)
    first_touch = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="scheduled first reach-out",
        scheduled_at="2099-12-31T00:00:00Z",
        context={"trigger": "enrollment_schedule"},
    )
    followup = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=contact.id,
        description="breakup touch",
        scheduled_at="2099-12-31T00:00:00Z",
        context={"trigger": "followup"},
    )

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="t-cancel",
        contact_id=contact.id,
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "lead@example.com", make_test_settings()
    )

    cancelled_followup = get_task(database_connection, followup.id)
    assert cancelled_followup is not None
    assert cancelled_followup.status == "cancelled"

    kept_first_touch = get_task(database_connection, first_touch.id)
    assert kept_first_touch is not None
    assert kept_first_touch.status == "pending"


def test_route_email_enrollment_idempotent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Routing a second email in the same thread doesn't fail on duplicate enrollment."""
    account = make_test_account(database_connection, email="wcidem@example.com")
    contact = make_test_contact(database_connection, email="repeat@example.com")
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

    # First inbound -> creates enrollment.
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

    # Second inbound -> should NOT raise on duplicate enrollment.
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


def test_route_email_emits_enrollment_added_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """A first-time enrollment must emit an enrollment_added activity tied
    to the contact, with workflow_id populated as a column."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection, email="wact@example.com")
    contact = make_test_contact(database_connection, email="sender@example.com")
    workflow = make_test_workflow(
        database_connection,
        account_id=account.id,
        workflow_type="inbound",
        name="inbound-inquiry",
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-wact",
        workflow_id=workflow.id,
        is_routed=True,
    )
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="t-wact",
        contact_id=contact.id,
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "sender@example.com", make_test_settings()
    )

    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="enrollment_added"
    )
    assert len(activities) == 1
    assert workflow.name in activities[0].summary
    row = database_connection.execute(
        "SELECT workflow_id FROM activity WHERE type = 'enrollment_added' "
        "AND contact_id = %s",
        (contact.id,),
    ).fetchone()
    assert row is not None
    assert row["workflow_id"] == workflow.id


def test_route_email_enrollment_added_only_once_on_duplicate_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When create_enrollment hits ON CONFLICT (returns None), no second
    enrollment_added activity should be emitted for the same pair."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection, email="wactdup@example.com")
    contact = make_test_contact(database_connection, email="repeat@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-wactdup",
        workflow_id=workflow.id,
        is_routed=True,
    )

    for index in (1, 2):
        inbound = create_email(
            database_connection,
            account_id=account.id,
            direction="inbound",
            subject=f"reply {index}",
            gmail_thread_id="t-wactdup",
            gmail_message_id=f"msg-wactdup-{index}",
            contact_id=contact.id,
        )
        assert inbound is not None
        route_email(
            database_connection, inbound, "repeat@example.com", make_test_settings()
        )

    activities = list_activities(
        database_connection, contact_id=contact.id, activity_type="enrollment_added"
    )
    assert len(activities) == 1


# -- Span contract: route_method attribute ------------------------------------


def _routing_spans(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        s
        for s in capfire.exporter.exported_spans_as_dict()
        if s["name"] == "routing.route_email"
    ]


def test_route_email_span_has_route_method_thread_match(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """routing.route_email span must set route_method='thread_match'."""
    account = make_test_account(database_connection, email="rmtm@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-rmtm",
        workflow_id=workflow.id,
        is_routed=True,
    )
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="t-rmtm",
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "sender@example.com", make_test_settings()
    )

    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "thread_match"


def test_route_email_span_has_route_method_unrouted(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """routing.route_email span must set route_method='unrouted' when the
    classifier ran on real candidates and rejected them all."""
    account = make_test_account(database_connection, email="rmur@example.com")
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
        gmail_thread_id="t-rmur",
    )
    assert new_email is not None

    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _function_model_returning(workflow_id=None, reasoning="no match")

    with classify_module._AGENT.override(model=model):  # pyright: ignore[reportPrivateUsage]
        route_email(database_connection, new_email, "nobody@example.com", settings)

    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "unrouted"


def test_route_email_span_has_route_method_skipped_no_inbound_workflows(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """routing.route_email span must set route_method='skipped_no_inbound_workflows'
    when the account has only outbound workflows -- classifier never runs."""
    account = make_test_account(database_connection, email="rmsni@example.com")
    outbound_wf = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-only",
        workflow_type="outbound",
    )
    update_workflow(
        database_connection,
        outbound_wf.id,
        goal="Cold outreach",
        instructions="Send cold emails",
    )
    activate_workflow(database_connection, outbound_wf.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="orphan",
        gmail_thread_id="t-rmsni",
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "nobody@example.com", make_test_settings()
    )

    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "skipped_no_inbound_workflows"
    assert "workflow_id" not in spans[0]["attributes"]


# -- RFC 2822 In-Reply-To fallback (Defect 2) ---------------------------------


def test_route_email_falls_back_to_rfc_message_id_match(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When the Gmail thread differs (Gmail re-threads on recipient side) the
    inbound's In-Reply-To must still resolve to the prior outbound's workflow.
    """
    account = make_test_account(database_connection, email="rfc1@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    # Prior outbound has the original Gmail thread id and a known Message-ID.
    prior = create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-outbound",
        rfc2822_message_id="<original@mailpilot.test>",
        workflow_id=workflow.id,
        is_routed=True,
    )
    assert prior is not None

    # Reply lands with a NEW gmail_thread_id (Gmail re-threaded on the
    # recipient side) but cites the original via In-Reply-To.
    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-reply-different",
        in_reply_to="<original@mailpilot.test>",
    )
    assert reply is not None

    routed = route_email(
        database_connection, reply, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id == workflow.id
    assert routed.is_routed is True


def test_route_email_falls_back_via_references_header(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """References header (multi-id chain) is also walked for the fallback."""
    account = make_test_account(database_connection, email="rfc2@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-out",
        rfc2822_message_id="<root@mailpilot.test>",
        workflow_id=workflow.id,
        is_routed=True,
    )

    # Inbound only carries References, no In-Reply-To.
    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-reply-x",
        references_header="<unrelated@mailpilot.test> <root@mailpilot.test>",
    )
    assert reply is not None

    routed = route_email(
        database_connection, reply, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id == workflow.id


def test_route_email_rfc_match_scoped_to_account(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """An RFC match in a different account must NOT leak the workflow."""
    account_a = make_test_account(database_connection, email="acc-a@example.com")
    account_b = make_test_account(database_connection, email="acc-b@example.com")
    workflow_a = make_test_workflow(
        database_connection, account_id=account_a.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow_a.id)

    # Outbound row exists on account A under workflow A.
    create_email(
        database_connection,
        account_id=account_a.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-cross",
        rfc2822_message_id="<shared@mailpilot.test>",
        workflow_id=workflow_a.id,
        is_routed=True,
    )

    # Inbound on account B cites the same Message-ID but must NOT pick up
    # account A's workflow.
    reply = create_email(
        database_connection,
        account_id=account_b.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-cross-b",
        in_reply_to="<shared@mailpilot.test>",
    )
    assert reply is not None

    routed = route_email(
        database_connection, reply, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id is None
    assert routed.is_routed is True


def test_route_email_span_has_route_method_rfc_message_id_match(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """routing.route_email span must set route_method='rfc_message_id_match'."""
    account = make_test_account(database_connection, email="rfcspan@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-x",
        rfc2822_message_id="<orig-span@mailpilot.test>",
        workflow_id=workflow.id,
        is_routed=True,
    )
    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-y",
        in_reply_to="<orig-span@mailpilot.test>",
    )
    assert reply is not None

    route_email(database_connection, reply, "alice@example.com", make_test_settings())

    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "rfc_message_id_match"
    assert spans[0]["attributes"]["workflow_id"] == workflow.id


def test_route_email_rfc_match_takes_precedence_over_thread(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When both signals are present, Message-ID wins over Gmail thread.

    Gmail may merge same-subject conversations across distinct outbound
    sends to one contact; In-Reply-To is the authoritative parent, so
    RFC match must beat thread match when they disagree (§V.27).
    """
    account = make_test_account(database_connection, email="prec@example.com")
    workflow_thread = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="thread-wf",
        workflow_type="inbound",
    )
    workflow_rfc = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="rfc-wf",
        workflow_type="inbound",
    )
    _activate_workflow(database_connection, workflow_thread.id)
    _activate_workflow(database_connection, workflow_rfc.id)

    # Same Gmail thread -> thread WF (wrong parent under a merge).
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="thread parent",
        gmail_thread_id="thread-shared",
        rfc2822_message_id="<thread-parent@mailpilot.test>",
        workflow_id=workflow_thread.id,
        is_routed=True,
    )
    # Different Gmail thread, but its Message-ID is what reply cites -> RFC WF.
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="rfc parent",
        gmail_thread_id="thread-other",
        rfc2822_message_id="<rfc-parent@mailpilot.test>",
        workflow_id=workflow_rfc.id,
        is_routed=True,
    )

    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: ambiguous",
        gmail_thread_id="thread-shared",
        in_reply_to="<rfc-parent@mailpilot.test>",
    )
    assert reply is not None

    routed = route_email(
        database_connection, reply, "alice@example.com", make_test_settings()
    )

    assert routed.workflow_id == workflow_rfc.id
    assert routed.route_method == "rfc_message_id_match"


def test_route_email_rfc_match_no_referenced_ids_falls_through(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """No In-Reply-To and no References -> RFC step yields nothing, classify runs."""
    account = make_test_account(database_connection, email="rfcnone@example.com")
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="bare",
        gmail_thread_id="t-bare",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "alice@example.com", make_test_settings()
    )

    # No active workflows -> unrouted, but importantly no exception raised.
    assert routed.is_routed is True
    assert routed.workflow_id is None


# -- Persisted route_method (§I email projection, §T.70) ----------------------


def test_route_email_persists_route_method_thread_match(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Email.route_method must equal the span attribute on thread_match."""
    account = make_test_account(database_connection, email="rmpt@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="prior",
        gmail_thread_id="t-rmpt",
        workflow_id=workflow.id,
        is_routed=True,
    )
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="reply",
        gmail_thread_id="t-rmpt",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "sender@example.com", make_test_settings()
    )

    assert routed.route_method == "thread_match"
    persisted = get_email(database_connection, new_email.id)
    assert persisted is not None
    assert persisted.route_method == "thread_match"


def test_route_email_persists_route_method_rfc_message_id_match(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Email.route_method must equal the span attribute on rfc_message_id_match."""
    account = make_test_account(database_connection, email="rmprfc@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="initial",
        gmail_thread_id="thread-rfc-out",
        rfc2822_message_id="<rmprfc@mailpilot.test>",
        workflow_id=workflow.id,
        is_routed=True,
    )
    reply = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: initial",
        gmail_thread_id="thread-rfc-in",
        in_reply_to="<rmprfc@mailpilot.test>",
    )
    assert reply is not None

    routed = route_email(
        database_connection, reply, "alice@example.com", make_test_settings()
    )

    assert routed.route_method == "rfc_message_id_match"
    persisted = get_email(database_connection, reply.id)
    assert persisted is not None
    assert persisted.route_method == "rfc_message_id_match"


def test_route_email_persists_route_method_classified(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Email.route_method must equal 'classified' when LLM returns a workflow."""
    account = make_test_account(database_connection, email="rmpc@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Question about support",
        body_text="please help",
        gmail_thread_id="t-rmpc",
    )
    assert new_email is not None

    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _function_model_returning(workflow_id=workflow.id, reasoning="match")

    with classify_module._AGENT.override(model=model):  # pyright: ignore[reportPrivateUsage]
        routed = route_email(
            database_connection, new_email, "asker@example.com", settings
        )

    assert routed.route_method == "classified"
    persisted = get_email(database_connection, new_email.id)
    assert persisted is not None
    assert persisted.route_method == "classified"


def test_route_email_classifier_rejects_persists_null_route_method(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Classifier-ran-no-match -> persisted route_method NULL (is_routed=TRUE).

    "unrouted" is a span-only label per §V.20: persisted enum admits only the
    7 decision values; NULL carries the "pipeline ran, no enum bucket matched"
    signal alongside is_routed=TRUE.
    """
    account = make_test_account(database_connection, email="rmpur@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="spam",
        body_text="prize",
        gmail_thread_id="t-rmpur",
    )
    assert new_email is not None

    settings = make_test_settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        anthropic_model="claude-sonnet-5",
    )
    model = _function_model_returning(workflow_id=None, reasoning="no match")

    with classify_module._AGENT.override(model=model):  # pyright: ignore[reportPrivateUsage]
        routed = route_email(
            database_connection, new_email, "nobody@example.com", settings
        )

    assert routed.route_method is None
    assert routed.is_routed is True
    persisted = get_email(database_connection, new_email.id)
    assert persisted is not None
    assert persisted.route_method is None
    assert persisted.is_routed is True


def test_route_email_persists_route_method_skipped_no_inbound_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Email.route_method must equal 'skipped_no_inbound_workflows' when account
    has only outbound workflows."""
    account = make_test_account(database_connection, email="rmpsni@example.com")
    outbound_wf = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="outbound-only",
        workflow_type="outbound",
    )
    update_workflow(
        database_connection,
        outbound_wf.id,
        goal="Cold outreach",
        instructions="Send cold emails",
    )
    activate_workflow(database_connection, outbound_wf.id)

    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="orphan",
        gmail_thread_id="t-rmpsni",
    )
    assert new_email is not None

    routed = route_email(
        database_connection, new_email, "nobody@example.com", make_test_settings()
    )

    assert routed.route_method == "skipped_no_inbound_workflows"
    persisted = get_email(database_connection, new_email.id)
    assert persisted is not None
    assert persisted.route_method == "skipped_no_inbound_workflows"


# -- Thread-alias inbound bind (§V.164 / §B.134) --------------------------------


def test_route_email_thread_alias_from_binds_enrolled_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.164: inbound on an outbound thread binds the enrolled contact.

    Fixture: T1 to a@domain, reply from afull@domain on the same thread.
    The From local-part alias must not stay bound or enrolled.
    """
    account = make_test_account(database_connection, email="alias-bind@example.com")
    workflow = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="thread-alias-bind",
        workflow_type="outbound",
    )
    enrolled = make_test_contact(database_connection, email="a@example.com")
    alias = make_test_contact(database_connection, email="afull@example.com")
    enrollment = make_test_enrollment(database_connection, workflow.id, enrolled.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-alias-bind",
        contact_id=enrolled.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
    )
    t2 = create_task(
        database_connection,
        enrollment_id=enrollment.id,
        workflow_id=workflow.id,
        contact_id=enrolled.id,
        description="Touch 2 of 3",
        scheduled_at=_FAR_FUTURE,
        context={"touch": 2, "trigger": "followup"},
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Automatic reply: retired",
        body_text=(
            "I have retired from the company. Please update your records "
            "and reach Janice@example.com or Alec@example.com instead."
        ),
        gmail_thread_id="t-alias-bind",
        contact_id=alias.id,
        sender="afull@example.com",
    )
    assert inbound is not None

    routed = route_email(
        database_connection, inbound, "afull@example.com", make_test_settings()
    )

    assert routed.contact_id == enrolled.id
    persisted = get_email(database_connection, inbound.id)
    assert persisted is not None
    assert persisted.contact_id == enrolled.id
    assert get_enrollment(database_connection, workflow.id, alias.id) is None
    still = get_enrollment(database_connection, workflow.id, enrolled.id)
    assert still is not None
    cancelled = get_task(database_connection, t2.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert get_contact_by_email(database_connection, "janice@example.com") is None
    assert get_contact_by_email(database_connection, "alec@example.com") is None


def test_route_email_rfc_alias_from_binds_enrolled_contact(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.164 / §V.27: RFC In-Reply-To binds the enrolled contact across rethread."""
    account = make_test_account(database_connection, email="alias-rfc@example.com")
    workflow = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="thread-alias-rfc",
        workflow_type="outbound",
    )
    enrolled = make_test_contact(database_connection, email="a@rfc.example.com")
    alias = make_test_contact(database_connection, email="afull@rfc.example.com")
    make_test_enrollment(database_connection, workflow.id, enrolled.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-alias-rfc-out",
        contact_id=enrolled.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
        rfc2822_message_id="<t1-alias-rfc@mail>",
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Touch 1",
        gmail_thread_id="t-alias-rfc-in",
        contact_id=alias.id,
        in_reply_to="<t1-alias-rfc@mail>",
        sender="afull@rfc.example.com",
    )
    assert inbound is not None

    routed = route_email(
        database_connection,
        inbound,
        "afull@rfc.example.com",
        make_test_settings(),
    )

    assert routed.contact_id == enrolled.id
    assert routed.route_method == "rfc_message_id_match"
    assert get_enrollment(database_connection, workflow.id, alias.id) is None


def test_route_email_thread_alias_same_from_unchanged(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.164 vs §V.161: same-contact reply keeps the enrolled contact_id."""
    account = make_test_account(database_connection, email="alias-same@example.com")
    workflow = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="thread-alias-same",
        workflow_type="outbound",
    )
    enrolled = make_test_contact(database_connection, email="a@same.example.com")
    make_test_enrollment(database_connection, workflow.id, enrolled.id)
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-alias-same",
        contact_id=enrolled.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Touch 1",
        gmail_thread_id="t-alias-same",
        contact_id=enrolled.id,
        sender="a@same.example.com",
    )
    assert inbound is not None

    routed = route_email(
        database_connection, inbound, "a@same.example.com", make_test_settings()
    )

    assert routed.contact_id == enrolled.id
    assert get_enrollment(database_connection, workflow.id, enrolled.id) is not None


# -- Skip marks + RFC parent cache --------------------------------------------


def test_mark_routed_persists_skip_method(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Shared mark_routed writes is_routed + route_method for skip marks."""
    account = make_test_account(database_connection, email="mark-routed@example.com")
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="old",
        gmail_thread_id="t-mark-routed",
    )
    assert email is not None

    marked = mark_routed(database_connection, email, "skipped_outside_window")

    assert marked.is_routed is True
    assert marked.route_method == "skipped_outside_window"
    persisted = get_email(database_connection, email.id)
    assert persisted is not None
    assert persisted.is_routed is True
    assert persisted.route_method == "skipped_outside_window"


def test_route_email_skip_outside_window(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Recency skip is owned by route_email, not a sync-layer span."""
    account = make_test_account(database_connection, email="skip-old@example.com")
    old = datetime.now(UTC) - timedelta(days=30)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="stale",
        gmail_thread_id="t-skip-old",
        received_at=old,
    )
    assert email is not None

    routed = route_email(
        database_connection,
        email,
        "alice@example.com",
        make_test_settings(),
        routing=RoutingContext(recency_cutoff=datetime.now(UTC) - timedelta(days=7)),
    )

    assert routed.is_routed is True
    assert routed.route_method == "skipped_outside_window"
    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "skipped_outside_window"


def test_route_email_skip_no_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Zero active workflows of any type → skipped_no_workflows via route_email."""
    account = make_test_account(database_connection, email="skip-nowf@example.com")
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="fresh",
        gmail_thread_id="t-skip-nowf",
        received_at=datetime.now(UTC),
    )
    assert email is not None

    routed = route_email(
        database_connection,
        email,
        "alice@example.com",
        make_test_settings(),
        routing=RoutingContext(has_active_workflows=False),
    )

    assert routed.is_routed is True
    assert routed.route_method == "skipped_no_workflows"


def test_route_email_skip_predates_workflows(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """received_at before earliest active workflow → skipped_predates_workflows."""
    account = make_test_account(database_connection, email="skip-pre@example.com")
    created = datetime.now(UTC)
    email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="before-wf",
        gmail_thread_id="t-skip-pre",
        received_at=created - timedelta(hours=1),
    )
    assert email is not None

    routed = route_email(
        database_connection,
        email,
        "alice@example.com",
        make_test_settings(),
        routing=RoutingContext(
            has_active_workflows=True,
            earliest_workflow_at=created,
        ),
    )

    assert routed.is_routed is True
    assert routed.route_method == "skipped_predates_workflows"


def test_route_email_rfc_parent_lookup_once_per_headers(
    database_connection: psycopg.Connection[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_try_rfc_message_id_match and thread-contact bind share one RFC lookup."""
    import mailpilot.routing as routing_module

    account = make_test_account(database_connection, email="rfc-once@example.com")
    workflow = make_test_workflow(
        database_connection, account_id=account.id, workflow_type="inbound"
    )
    _activate_workflow(database_connection, workflow.id)
    enrolled = make_test_contact(database_connection, email="a@rfc-once.example.com")
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="Touch 1",
        gmail_thread_id="t-rfc-once-out",
        contact_id=enrolled.id,
        workflow_id=workflow.id,
        status="sent",
        is_routed=True,
        rfc2822_message_id="<t1-rfc-once@mail>",
    )
    inbound = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="Re: Touch 1",
        gmail_thread_id="t-rfc-once-in",
        contact_id=enrolled.id,
        in_reply_to="<t1-rfc-once@mail>",
        sender="a@rfc-once.example.com",
    )
    assert inbound is not None

    calls: list[object] = []
    real = routing_module.find_email_by_rfc2822_message_id

    def spy(
        connection: psycopg.Connection[dict[str, Any]],
        account_id: str,
        message_ids: list[str],
    ) -> object:
        calls.append(tuple(message_ids))
        return real(connection, account_id, message_ids)

    monkeypatch.setattr(routing_module, "find_email_by_rfc2822_message_id", spy)

    routed = route_email(
        database_connection,
        inbound,
        "a@rfc-once.example.com",
        make_test_settings(),
    )

    assert routed.route_method == "rfc_message_id_match"
    assert len(calls) == 1


def test_thread_contact_cache_keys_on_references(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Same thread + missing In-Reply-To, different References → two binds."""
    account = make_test_account(database_connection, email="cache-refs@example.com")
    workflow_a = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="cache-refs-a",
        workflow_type="outbound",
    )
    workflow_b = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="cache-refs-b",
        workflow_type="outbound",
    )
    contact_a = make_test_contact(database_connection, email="a@cache-refs.example.com")
    contact_b = make_test_contact(database_connection, email="b@cache-refs.example.com")
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="A",
        gmail_thread_id="t-merged",
        contact_id=contact_a.id,
        workflow_id=workflow_a.id,
        status="sent",
        is_routed=True,
        rfc2822_message_id="<a@cache-refs>",
    )
    create_email(
        database_connection,
        account_id=account.id,
        direction="outbound",
        subject="B",
        gmail_thread_id="t-merged",
        contact_id=contact_b.id,
        workflow_id=workflow_b.id,
        status="sent",
        is_routed=True,
        rfc2822_message_id="<b@cache-refs>",
    )
    ctx = RoutingContext()

    first = find_thread_enrolled_contact(
        database_connection,
        account.id,
        gmail_thread_id="t-merged",
        in_reply_to=None,
        references_header="<a@cache-refs>",
        routing=ctx,
    )
    second = find_thread_enrolled_contact(
        database_connection,
        account.id,
        gmail_thread_id="t-merged",
        in_reply_to=None,
        references_header="<b@cache-refs>",
        routing=ctx,
    )

    assert first is not None
    assert first.id == contact_a.id
    assert second is not None
    assert second.id == contact_b.id
    assert len(ctx.thread_contacts) == 2


def _xai_incorrect_api_key() -> Exception:
    """xAI present-but-wrong key as raised by pydantic-ai (§B.152)."""
    from pydantic_ai.exceptions import ModelAPIError

    return ModelAPIError(
        "grok-4.5",
        "Incorrect API key provided. You can obtain an API key from https://console.x.ai.",
    )


def _inbound_unthreaded_email(
    connection: psycopg.Connection[dict[str, Any]],
    *,
    account_email: str,
    thread_id: str,
) -> Any:
    """Inbound email that falls through to LLM classify."""
    account = make_test_account(connection, email=account_email)
    workflow = make_test_workflow(
        connection,
        account_id=account.id,
        name=f"wf-{thread_id}",
        workflow_type="inbound",
    )
    _activate_workflow(connection, workflow.id)
    email = create_email(
        connection,
        account_id=account.id,
        direction="inbound",
        subject="Pricing question",
        body_text="How much does your product cost?",
        gmail_thread_id=thread_id,
    )
    assert email is not None
    return email


def test_route_email_invalid_key_logs_error_not_exception(
    capsys: pytest.CaptureFixture[str],
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: classify invalid-key is host-config; no Traceback, email stays unrouted."""
    from mailpilot.run import remaining_drain_is_skipped

    email = _inbound_unthreaded_email(
        database_connection,
        account_email="classify-badkey@example.com",
        thread_id="t-classify-badkey",
    )
    settings = make_test_settings(llm_provider="xai", xai_api_key="xai-wrong")

    with (
        patch(
            "mailpilot.routing.classify_email",
            side_effect=_xai_incorrect_api_key(),
        ),
        patch("mailpilot.routing.logfire.exception") as mock_exception,
        patch("mailpilot.routing.logfire.error") as mock_error,
    ):
        routed = route_email(database_connection, email, "alice@example.com", settings)

    assert routed.is_routed is False
    assert remaining_drain_is_skipped() is True
    mock_exception.assert_not_called()
    mock_error.assert_called_once()
    assert mock_error.call_args.args[0] == "run.provider_key.invalid"
    err = capsys.readouterr().err
    assert err.count("event=error") == 1
    assert "mailpilot config set xai_api_key" in err
    assert "Traceback" not in err


def test_route_email_invalid_key_skips_remaining_classify(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: after invalid-key classify, remaining inbound this tick skip LLM."""
    first = _inbound_unthreaded_email(
        database_connection,
        account_email="classify-skip1@example.com",
        thread_id="t-classify-skip-1",
    )
    second = _inbound_unthreaded_email(
        database_connection,
        account_email="classify-skip2@example.com",
        thread_id="t-classify-skip-2",
    )
    settings = make_test_settings(llm_provider="xai", xai_api_key="xai-wrong")

    with patch(
        "mailpilot.routing.classify_email",
        side_effect=_xai_incorrect_api_key(),
    ) as mock_classify:
        route_email(database_connection, first, "a@example.com", settings)
        route_email(database_connection, second, "b@example.com", settings)

    mock_classify.assert_called_once()
