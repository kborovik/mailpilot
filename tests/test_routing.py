"""Tests for the email routing pipeline (ADR-04)."""

from __future__ import annotations

from typing import Any

import psycopg
from logfire.testing import CaptureLogfire
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
    get_enrollment,
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


# -- enrollment creation -------------------------------------------------------


def test_route_email_creates_enrollment_on_route(
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
    assert enrollment.status == "pending"


def test_route_email_enrollment_idempotent(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """Routing a second email in the same thread doesn't fail on duplicate enrollment."""
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


def test_route_email_emits_workflow_assigned_activity(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """A first-time enrollment must emit a workflow_assigned activity tied
    to the contact, with workflow id + name in the detail."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection, email="wact@example.com")
    contact = make_test_contact(
        database_connection, email="sender@example.com", domain="example.com"
    )
    workflow = make_test_workflow(
        database_connection,
        account_id=account.id,
        workflow_type="inbound",
        name="Inbound Inquiry",
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
        database_connection, contact_id=contact.id, activity_type="workflow_assigned"
    )
    assert len(activities) == 1
    assert workflow.name in activities[0].summary


def test_route_email_workflow_assigned_only_once_on_duplicate_enrollment(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When create_enrollment hits ON CONFLICT (returns None), no second
    workflow_assigned activity should be emitted for the same pair."""
    from mailpilot.database import list_activities

    account = make_test_account(database_connection, email="wactdup@example.com")
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
        database_connection, contact_id=contact.id, activity_type="workflow_assigned"
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
    """routing.route_email span must set route_method='unrouted'."""
    account = make_test_account(database_connection, email="rmur@example.com")
    new_email = create_email(
        database_connection,
        account_id=account.id,
        direction="inbound",
        subject="orphan",
        gmail_thread_id="t-rmur",
    )
    assert new_email is not None

    route_email(
        database_connection, new_email, "nobody@example.com", make_test_settings()
    )

    spans = _routing_spans(capfire)
    assert len(spans) == 1
    assert spans[0]["attributes"]["route_method"] == "unrouted"


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


def test_route_email_thread_match_takes_precedence_over_rfc(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """When both signals are present, thread match wins (cheaper, no
    cross-side ambiguity)."""
    account = make_test_account(database_connection, email="prec@example.com")
    workflow_thread = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="Thread WF",
        workflow_type="inbound",
    )
    workflow_rfc = make_test_workflow(
        database_connection,
        account_id=account.id,
        name="RFC WF",
        workflow_type="inbound",
    )
    _activate_workflow(database_connection, workflow_thread.id)
    _activate_workflow(database_connection, workflow_rfc.id)

    # Same Gmail thread -> thread WF.
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

    assert routed.workflow_id == workflow_thread.id


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
