"""Workflow template registry.

A workflow template owns the agent's shape: the tool set bound to the
Pydantic AI Agent and the system-prompt protocol composed from named
fragments. Workflows pick a template by name; ``workflow.type`` is
populated server-side from the template's declared direction at insert
time.

Templates are code-defined constants. Adding a new template (e.g.
``inbound-postgres``) is a code change + PR, not a workflow update.

See SPEC.md sections §V.44 (registry shape), §V.45 (composition / ownership),
§V.46 (naming convention).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from pydantic_ai import Tool

from mailpilot.agent.invoke import (
    AgentDeps,
    _wrap_cancel_task,  # pyright: ignore[reportPrivateUsage]
    _wrap_conclude_enrollment,  # pyright: ignore[reportPrivateUsage]
    _wrap_create_task,  # pyright: ignore[reportPrivateUsage]
    _wrap_disable_contact,  # pyright: ignore[reportPrivateUsage]
    _wrap_list_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_list_enrollments,  # pyright: ignore[reportPrivateUsage]
    _wrap_noop,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_email,  # pyright: ignore[reportPrivateUsage]
    _wrap_reply_email,  # pyright: ignore[reportPrivateUsage]
    _wrap_search_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_search_emails,  # pyright: ignore[reportPrivateUsage]
    _wrap_send_email,  # pyright: ignore[reportPrivateUsage]
)
from mailpilot.models import WorkflowTemplateName, WorkflowType


@dataclass(frozen=True)
class WorkflowTemplate:
    """Named binding of agent tools + protocol composed from fragments.

    ``build_protocol`` composes the tool-loop system prompt. The deferred-task
    fragment is direction-scoped (§V.31): an inbound template uses
    _DEFERRED_TASK_INBOUND (reply once, then stop; the system records the
    outcome -- inbound binds neither ``conclude_enrollment`` nor
    ``create_task``); an outbound template uses _DEFERRED_TASK_TASK
    (terminal-outcome instruction). The outbound first reach-out is no longer a
    tool-loop trigger -- it is a compose-only touch run (§V.136,
    _TOUCH_COMPOSE), so the initial-send-only fragment was retired and
    ``trigger`` no longer selects the branch. Canonical fragment order per
    §V.45: _BASE -> _DEFERRED_TASK_<branch> -> _MUST_SEND -> _DECLINE ->
    _NO_FABRICATION. Per §V.41 there is no workflow-specific overlay fragment;
    KB-grounding discipline lives in workflow.instructions.
    """

    name: WorkflowTemplateName
    direction: WorkflowType
    description: str
    protocol_pre: str
    protocol_post: str
    tools: tuple[Tool[AgentDeps], ...]

    def build_protocol(self, trigger: str = "task") -> str:
        """Compose the tool-loop protocol per direction (§V.31, §V.45).

        Inbound templates use the inbound-reply branch (reply once, then stop;
        the system records the outcome); outbound templates use the
        terminal-outcome branch (conclude the enrollment when the goal is met).
        The branch is direction-only: the outbound first reach-out moved to the
        compose-only touch path (§V.136), so ``trigger`` no longer selects a
        fragment. It is retained on the signature for call-site compatibility
        (``template view`` + tests still pass it).
        """
        del trigger  # branch is direction-only after §V.136 retired _INITIAL
        deferred = (
            _DEFERRED_TASK_INBOUND
            if self.direction == "inbound"
            else _DEFERRED_TASK_TASK
        )
        return self.protocol_pre + deferred + self.protocol_post

    @property
    def protocol(self) -> str:
        """Default protocol. Used by ``template view`` CLI."""
        return self.build_protocol()


# -- Protocol fragments --------------------------------------------------------
# Each fragment owns exactly one project-wide rule. Amending the rule = edit
# one constant; every template that composes the fragment picks up the change.


# _BASE carries the email-universal scaffolding composed into every template's
# protocol_pre: brief-summary, the trigger-email-already-provided nudge, and the
# personalize-from-the-pre-fed-records directive (§V.8, §V.135). The contact and
# company records (with their recent notes) are now mechanically pre-fed into the
# user prompt by invoke_workflow_agent via load_contact_view / load_company_view
# (§V.135), so the agent no longer fetches them through a read tool. _BASE names
# no read tool at all -- naming exactly one would trip §V.40 (a fragment names 0
# or >=2 tools). Per §V.45 the prompt string itself carries no §-cite: the runtime
# agent has no SPEC.md, so governing invariants are named here in the comment, not
# in the model-visible text (closes §B.79).
_BASE = (
    "Keep your final summary brief (2-3 sentences, plain text, no emojis).\n"
    "When a trigger email is included in your prompt, its full body is "
    "already provided; you do not need to fetch it again.\n"
    "The contact and company records for this thread, including their most "
    "recent notes, are provided in your prompt. Treat those notes as context "
    "for personalizing your response. Never invent facts about a contact or "
    "company that are not supported by their records.\n"
)

# Product-spec multi-row facts use list/prose shape only (§V.42). Product-spec
# answering is an inbound knowledge-base concern (§B.114), so this fragment is
# composed into the inbound templates' protocol_pre only, never outbound-general
# (§V.45). No table mandate; runtime body format lint retired (§B.128). Per
# §V.45 the model-visible string carries no §-cite.
_PRODUCT_SPECS = (
    "When the reply body carries product specifications (model numbers, flow "
    "rates, dimensions, capacities), present each fact as its own plain-text "
    "line or bullet list item (start the line with '- '). Keep one fact per "
    "line.\n"
)

# meeting_booked is the "I already booked" reply path only (§V.127 tool docs +
# §V.128 calendar detection). Mere interest or sharing a calendar link is NOT
# goal-met -- leave the enrollment open so calendar ingest can conclude. A
# looser "when the goal is met" line made agents stamp meeting_booked on
# "I'd like to book" replies and fail campaign-test booked scenarios.
# Address-change / hard email-redirect auto-replies are a hard stop (§V.161,
# closes §B.131): continuing touches to the old address is wrong. OOO /
# temporary-absence auto-replies are different -- noop only, no terminal.
# Per §V.45 the model-visible string carries no §-cite.
_DEFERRED_TASK_TASK = (
    "After achieving the workflow goal for a contact, conclude the enrollment "
    "by calling conclude_enrollment with a disposition and a brief note. Use "
    "meeting_booked only when the contact confirms they already booked or the "
    "meeting is already scheduled -- sharing a calendar link or receiving mere "
    "interest is not enough; leave the enrollment open so calendar detection "
    "can record the booking. When the contact wants to talk or asks for times, "
    "reply_email with the calendar link (or offered times) and leave the "
    "enrollment open -- still end the turn with a tool call; do not stop "
    "without tools. Use do_not_contact when the contact asks not to be reached, "
    "when a human says wrong person, or when an automated reply says the email "
    "address has changed, to update your records, or hard-redirects to a new "
    "address -- stop further touches to the old address; put the redirect and "
    "the new email (when present) in the note so campaign review can re-target; "
    "never enroll the new address yourself. Out-of-office and temporary absence "
    "auto-replies are different: call noop (pause once, leave enrollment open) "
    "-- do not conclude and do not reply. "
    "Use contact_later to defer (optionally pass reschedule_at). If work cannot "
    "complete now but should resume later, schedule a deferred task via "
    "create_task with a future scheduled_at.\n"
)

# _TOUCH_COMPOSE is the compose-only touch protocol (§V.136). An outbound touch
# run -- the first reach-out or a system-scheduled follow-up in a workflow's
# cadence -- returns a structured message instead of calling a send tool: the
# harness sends it via email_ops and schedules any further touch, so the send is
# structural (§V.120) and the run is exempt from the tool-use enforcement (§V.81).
# The fragment names no tool (the compose-only agent binds none), so §V.40 does
# not apply. Personalization points at the mechanically pre-fed contact/company
# records (§V.135). Per §V.45 the model-visible string carries no SPEC cite, and
# it is ASCII-only per §C.
_TOUCH_COMPOSE = (
    "You are composing one outbound email in a cold-outreach sequence for this "
    "contact. Write a brief, personalized message grounded only in the contact "
    "and company records provided in your prompt; never invent facts they do "
    "not support.\n"
    "Return the message as structured output with a subject and a body. Provide "
    "a subject for the first message that opens a new thread; on a later "
    "follow-up that continues an existing thread you may leave the subject "
    "empty to keep the thread's subject. Write the body as plain text with no "
    "emojis.\n"
    "The system sends the message and schedules any further follow-up -- you do "
    "not send anything or schedule anything yourself.\n"
)

# _DEFERRED_TASK_INBOUND is the inbound branch (§V.31, closes §B.124). An
# inbound auto-reply answers the incoming thread once and stops: the system
# records the enrollment outcome after the reply, and an inbound reply never
# schedules its own follow-up work. Inbound templates bind neither
# conclude_enrollment nor create_task (see _INBOUND_CORE), so this fragment
# names no tool -- the tool roster is the structural guard and the prompt just
# frames the single-reply lifecycle. Composing the outbound TASK / INITIAL
# branch into an inbound template was §B.124: the TASK branch ordered a
# conclude_enrollment the workflow forbids, and the INITIAL branch mislabelled
# the reply as a first reach-out. Per §V.45 the model-visible string carries no
# §-cite.
_DEFERRED_TASK_INBOUND = (
    "Reply to the inbound email once, then stop. The system records the "
    "enrollment outcome after your reply -- you do not record a disposition "
    "or schedule any follow-up work yourself.\n"
)

# _MUST_SEND is the email-universal prompt-side mirror of the §V.120 runtime
# reply guard (_sent_reply in invoke.py). A trigger turn reaches the recipient
# only through a reply_email / send_email tool call -- drafting the body in
# reasoning sends nothing -- so the model is told to end every trigger turn in a
# real send or an explicit noop. Per §V.45 the fragment lives in the template
# (must-send is direction-universal mechanics, not workflow-specific policy) and
# the model-visible string carries no §-cite; the governing invariant is named
# here in the comment. Names three tools, satisfying the >=2-distinct-tool rule
# (§V.40).
_MUST_SEND = (
    "End every trigger turn with a tool call: reply_email to answer an inbound "
    "thread, send_email to open an outbound one, or noop to decline explicitly. "
    "Drafting the message in your reasoning does not send it -- the recipient "
    "sees it only through a reply_email or send_email call.\n"
)

_DECLINE = (
    "If no available information is relevant to the question, reply with a "
    "polite decline that does not invent facts.\n"
)

# _FALLBACK_ACKNOWLEDGEMENT is the fixed, content-free body the run loop sends
# when an inbound-email task fails terminally and no reply was emitted this run
# (§V.131, closes §B.116). It is code-defined -- never model-generated and never
# partial -- so the grounding risk the agent failed on (a fabricated or truncated
# answer, §V.45 / §V.130) cannot reach the sender; the inbound sender gets a brief
# receipt instead of silent NO_REPLY. ``run._handle_agent_failure`` sends this
# string verbatim via ``email_ops.reply_email``; an outbound first reach-out
# failure (no inbound email_id) stays silent. The model never sees this string,
# yet it stays ASCII-only per §C (code-defined content, not agent-generated).
_FALLBACK_ACKNOWLEDGEMENT = (
    "Thank you for your message. We have received it and a member of our team "
    "will follow up with you shortly.\n"
)

_NO_FABRICATION = (
    "Never fabricate specifications, model numbers, prices, or claims that "
    "are not present in the available context. When uncertain, decline rather "
    "than guess.\n"
)

# Per §V.41 / §V.45 there is intentionally no Drive-grounding overlay fragment
# here. KB-grounding discipline (search-first, 2-search budget then a single
# list, read top >=3 hits, per-target search on compare) and verbatim-citation
# / no-unit-conversion guidance are workflow-specific behaviour, so they live
# in the workflow definition's ``instructions`` field (workflows/*.toml per
# §V.103), not a code-defined template fragment. The inbound-google-drive
# template only binds the Drive tool set (_DRIVE); its protocol is the same
# fragment-free composition as the non-Drive templates.


# -- Tool tuples ---------------------------------------------------------------
# _CORE = email + CRM + task + enrollment tools, no external data binding.
# _DRIVE = Google-Drive-specific KB grounding tools.


_CORE: tuple[Tool[AgentDeps], ...] = (
    Tool(_wrap_send_email, name="send_email"),
    Tool(_wrap_reply_email, name="reply_email"),
    Tool(_wrap_create_task, name="create_task"),
    Tool(_wrap_cancel_task, name="cancel_task"),
    Tool(_wrap_conclude_enrollment, name="conclude_enrollment"),
    Tool(_wrap_disable_contact, name="disable_contact"),
    Tool(_wrap_list_enrollments, name="list_enrollments"),
    Tool(_wrap_search_emails, name="search_emails"),
    Tool(_wrap_read_email, name="read_email"),
    Tool(_wrap_noop, name="noop"),
)

# Inbound templates bind neither conclude_enrollment nor create_task (§V.31,
# closes §B.124): the system records the enrollment outcome after an inbound
# auto-reply, and an inbound reply schedules no follow-up of its own. The
# inbound roster is _CORE minus those two outbound-lifecycle tools -- derived
# from _CORE so the shared tools stay defined once (a new _CORE tool joins the
# inbound roster automatically unless it is an outbound-lifecycle tool added to
# the excluded set).
_INBOUND_EXCLUDED_TOOLS = frozenset({"conclude_enrollment", "create_task"})
_INBOUND_CORE: tuple[Tool[AgentDeps], ...] = tuple(
    tool for tool in _CORE if tool.name not in _INBOUND_EXCLUDED_TOOLS
)

# Per §V.38: each Drive tool binds a googleapiclient.discovery.Resource that
# carries one shared httplib2.Http transport with no internal locks. Pydantic
# AI dispatches sync tools via asyncio.to_thread, so an Anthropic-emitted
# parallel fan-out would land two threads against the same Http and race the
# httplib2 connection-pool dict (see §B.34 -- one read returned in ~1.1s while
# its sibling hung 60.8s at the socket timeout). sequential=True tells the
# dispatcher to serialize parallel emissions on these tools; non-Drive peer
# tools keep parallel dispatch. Contract test in
# tests/test_agent_drive_concurrency.py enumerates these registrations so a
# future drop of the kwarg trips the suite.
_DRIVE: tuple[Tool[AgentDeps], ...] = (
    Tool(_wrap_list_drive_markdown, name="list_drive_markdown", sequential=True),
    Tool(_wrap_read_drive_markdown, name="read_drive_markdown", sequential=True),
    Tool(_wrap_search_drive_markdown, name="search_drive_markdown", sequential=True),
)


# -- Template registry ---------------------------------------------------------


TEMPLATES: dict[WorkflowTemplateName, WorkflowTemplate] = {
    "outbound-general": WorkflowTemplate(
        name="outbound-general",
        direction="outbound",
        description="Outbound email/CRM workflow without external knowledge base.",
        protocol_pre=_BASE,
        protocol_post=_MUST_SEND + _DECLINE + _NO_FABRICATION,
        tools=_CORE,
    ),
    "inbound-general": WorkflowTemplate(
        name="inbound-general",
        direction="inbound",
        description="Inbound auto-reply workflow without external knowledge base.",
        protocol_pre=_BASE + _PRODUCT_SPECS,
        protocol_post=_MUST_SEND + _DECLINE + _NO_FABRICATION,
        tools=_INBOUND_CORE,
    ),
    "inbound-google-drive": WorkflowTemplate(
        name="inbound-google-drive",
        direction="inbound",
        description=(
            "Inbound auto-reply grounded in a Google Drive Markdown knowledge base."
        ),
        protocol_pre=_BASE + _PRODUCT_SPECS,
        protocol_post=_MUST_SEND + _DECLINE + _NO_FABRICATION,
        tools=_INBOUND_CORE + _DRIVE,
    ),
}


def template_names() -> tuple[WorkflowTemplateName, ...]:
    """Return all defined template names (matches WorkflowTemplateName Literal)."""
    return get_args(WorkflowTemplateName)
