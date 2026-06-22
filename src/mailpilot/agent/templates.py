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
    _wrap_create_task,  # pyright: ignore[reportPrivateUsage]
    _wrap_disable_contact,  # pyright: ignore[reportPrivateUsage]
    _wrap_list_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_list_enrollments,  # pyright: ignore[reportPrivateUsage]
    _wrap_noop,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_company,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_contact,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_read_email,  # pyright: ignore[reportPrivateUsage]
    _wrap_record_enrollment_outcome,  # pyright: ignore[reportPrivateUsage]
    _wrap_reply_email,  # pyright: ignore[reportPrivateUsage]
    _wrap_search_drive_markdown,  # pyright: ignore[reportPrivateUsage]
    _wrap_search_emails,  # pyright: ignore[reportPrivateUsage]
    _wrap_send_email,  # pyright: ignore[reportPrivateUsage]
)
from mailpilot.models import WorkflowTemplateName, WorkflowType


@dataclass(frozen=True)
class WorkflowTemplate:
    """Named binding of agent tools + protocol composed from fragments.

    The deferred-task fragment is selected per-invocation by ``trigger``
    (§V.31): ``trigger='task'`` -> _DEFERRED_TASK_TASK (terminal-outcome
    instruction); other triggers (``enrollment_run``,
    ``enrollment_schedule`` per §V.32, ``manual``, ``email``) ->
    _DEFERRED_TASK_INITIAL (initial-send-only instruction; prevents
    premature ``record_enrollment_outcome`` on first reach-out).
    Canonical fragment order per §V.45: _BASE -> _DEFERRED_TASK_<branch> ->
    _MUST_SEND -> _DECLINE -> _NO_FABRICATION. Per §V.41 there is no
    workflow-specific overlay fragment; KB-grounding discipline lives in
    workflow.instructions.
    """

    name: WorkflowTemplateName
    direction: WorkflowType
    description: str
    protocol_pre: str
    protocol_post: str
    tools: tuple[Tool[AgentDeps], ...]

    def build_protocol(self, trigger: str) -> str:
        """Compose protocol per ``trigger`` per §V.31."""
        deferred = _DEFERRED_TASK_TASK if trigger == "task" else _DEFERRED_TASK_INITIAL
        return self.protocol_pre + deferred + self.protocol_post

    @property
    def protocol(self) -> str:
        """Default protocol (``trigger='task'``). Used by ``template view`` CLI."""
        return self.build_protocol("task")


# -- Protocol fragments --------------------------------------------------------
# Each fragment owns exactly one project-wide rule. Amending the rule = edit
# one constant; every template that composes the fragment picks up the change.


# _BASE mandates a GFM pipe table for product-spec rows; the outbound format
# lint (_check_spec_table in tools.py) rejects space-aligned spec blocks -- the
# pipe-table mandate is the primary enforcement, the lint a backstop (§V.42).
# Per §V.45 the prompt string itself carries no §-cite: the runtime reply agent
# has no SPEC.md, so the governing invariant is named here in the comment, not
# in the model-visible text (closes §B.79).
_BASE = (
    "Keep your final summary brief (2-3 sentences, plain text, no emojis).\n"
    "When the reply body carries product specifications (model numbers, flow "
    "rates, dimensions, capacities), you MUST present them as a "
    "GitHub-flavored Markdown pipe table with a header row and a |---| "
    "separator -- e.g. `| Specification | Value |` then `|---|---|` and one "
    "row per spec. Do not use space-aligned or single-spaced lines as a "
    "substitute; such spec blocks are rejected by the outbound format lint.\n"
    "When a trigger email is included in your prompt, its full body is "
    "already provided -- do not call read_email to fetch it again. The "
    "current contact's profile is also inlined, so do not call read_contact "
    "for the same address either.\n"
    "If read_contact or read_company returns notes or company_notes, treat "
    "them as context for personalizing your response. Never invent facts "
    "about a contact or company that aren't supported by their notes.\n"
)

_DEFERRED_TASK_TASK = (
    "After completing the workflow objective for a contact, call "
    "record_enrollment_outcome with outcome='completed' and a brief reason. "
    "If work cannot complete now but should resume later, schedule a deferred "
    "task via create_task with a future scheduled_at.\n"
)

_DEFERRED_TASK_INITIAL = (
    "Send the initial email and stop; do not call record_enrollment_outcome "
    "on this invocation. The outcome will be assessed when a reply arrives "
    "or when a follow-up task drains. If the initial send is not appropriate "
    "right now but should resume later, schedule a deferred task via "
    "create_task with a future scheduled_at.\n"
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
    "End every trigger turn by actually sending the message: call reply_email "
    "to answer an inbound thread, or send_email to open an outbound one. "
    "Drafting the message text in your reasoning is not sending it -- the "
    "message reaches the recipient only through a reply_email or send_email "
    "tool call. If no message is appropriate, call noop to decline explicitly. "
    "Never finish a turn having drafted a message without calling one of these "
    "tools.\n"
)

_DECLINE = (
    "If no available information is relevant to the question, reply with a "
    "polite decline that does not invent facts. The decline path still "
    "requires at least one tool call (e.g. reply_email to send the decline, "
    "or noop if no contact action is appropriate) to satisfy the tool-use "
    "contract.\n"
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
    Tool(_wrap_record_enrollment_outcome, name="record_enrollment_outcome"),
    Tool(_wrap_disable_contact, name="disable_contact"),
    Tool(_wrap_list_enrollments, name="list_enrollments"),
    Tool(_wrap_search_emails, name="search_emails"),
    Tool(_wrap_read_contact, name="read_contact"),
    Tool(_wrap_read_company, name="read_company"),
    Tool(_wrap_read_email, name="read_email"),
    Tool(_wrap_noop, name="noop"),
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
        protocol_pre=_BASE,
        protocol_post=_MUST_SEND + _DECLINE + _NO_FABRICATION,
        tools=_CORE,
    ),
    "inbound-google-drive": WorkflowTemplate(
        name="inbound-google-drive",
        direction="inbound",
        description=(
            "Inbound auto-reply grounded in a Google Drive Markdown knowledge base."
        ),
        protocol_pre=_BASE,
        protocol_post=_MUST_SEND + _DECLINE + _NO_FABRICATION,
        tools=_CORE + _DRIVE,
    ),
}


def template_names() -> tuple[WorkflowTemplateName, ...]:
    """Return all defined template names (matches WorkflowTemplateName Literal)."""
    return get_args(WorkflowTemplateName)
