"""Workflow template registry.

A workflow template owns the agent's shape: the tool set bound to the
Pydantic AI Agent and the system-prompt protocol composed from named
fragments. Workflows pick a template by name; ``workflow.type`` is
populated server-side from the template's declared direction at insert
time.

Templates are code-defined constants. Adding a new template (e.g.
``inbound-postgres``) is a code change + PR, not a workflow update.

See SPEC.md sections §V.32 (registry shape), §V.33 (composition / ownership),
§V.34 (naming convention).
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
    (§V.49): ``trigger='task'`` -> _DEFERRED_TASK_TASK (terminal-outcome
    instruction); other triggers (``enrollment_run``,
    ``enrollment_schedule`` per §V.55, ``manual``, ``email``) ->
    _DEFERRED_TASK_INITIAL (initial-send-only instruction; prevents
    premature ``record_enrollment_outcome`` on first reach-out).
    Canonical fragment order per §V.33: _BASE -> _DEFERRED_TASK_<branch> ->
    [overlay]? -> _DECLINE -> _NO_FABRICATION.
    """

    name: WorkflowTemplateName
    direction: WorkflowType
    description: str
    protocol_pre: str
    protocol_post: str
    tools: tuple[Tool[AgentDeps], ...]

    def build_protocol(self, trigger: str) -> str:
        """Compose protocol per ``trigger`` per §V.49."""
        deferred = _DEFERRED_TASK_TASK if trigger == "task" else _DEFERRED_TASK_INITIAL
        return self.protocol_pre + deferred + self.protocol_post

    @property
    def protocol(self) -> str:
        """Default protocol (``trigger='task'``). Used by ``template view`` CLI."""
        return self.build_protocol("task")


# -- Protocol fragments --------------------------------------------------------
# Each fragment owns exactly one project-wide rule. Amending the rule = edit
# one constant; every template that composes the fragment picks up the change.


_BASE = (
    "Keep your final summary brief (2-3 sentences, plain text, no emojis).\n"
    "Email bodies may use Markdown formatting (headers, bold, tables).\n"
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

_DRIVE_GROUNDING = (
    "Workflow instructions reference a Google Drive folder of Markdown "
    "notes. Ground every reply in that folder. Prefer "
    "search_drive_markdown(folder_id, query) to target the most relevant "
    "documents -- enumerating the whole folder with list_drive_markdown "
    "wastes context once the KB grows past a handful of files. When "
    "search_drive_markdown returns >=2 hits, call read_drive_markdown on the "
    "top >=3 results (or all hits if fewer than 3) before composing the "
    "reply, then pick the best match by model-number / spec match -- not "
    "ranking order alone. Cite the source file in the reply.\n"
)


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

# Per §V.61: each Drive tool binds a googleapiclient.discovery.Resource that
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
        protocol_post=_DECLINE + _NO_FABRICATION,
        tools=_CORE,
    ),
    "inbound-general": WorkflowTemplate(
        name="inbound-general",
        direction="inbound",
        description="Inbound auto-reply workflow without external knowledge base.",
        protocol_pre=_BASE,
        protocol_post=_DECLINE + _NO_FABRICATION,
        tools=_CORE,
    ),
    "inbound-google-drive": WorkflowTemplate(
        name="inbound-google-drive",
        direction="inbound",
        description=(
            "Inbound auto-reply grounded in a Google Drive Markdown knowledge base."
        ),
        protocol_pre=_BASE,
        protocol_post=_DRIVE_GROUNDING + _DECLINE + _NO_FABRICATION,
        tools=_CORE + _DRIVE,
    ),
}


def template_names() -> tuple[WorkflowTemplateName, ...]:
    """Return all defined template names (matches WorkflowTemplateName Literal)."""
    return get_args(WorkflowTemplateName)
