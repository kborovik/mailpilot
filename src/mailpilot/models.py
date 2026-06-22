"""Shared domain models mirroring schema.sql tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class Account(BaseModel):
    """Gmail account managed by MailPilot."""

    id: str
    email: str
    display_name: str = ""
    gmail_history_id: str | None = None
    watch_expiration: datetime | None = None
    last_synced_at: datetime | None = None
    disabled_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class AccountSummary(BaseModel):
    """List-view projection of `Account`.

    Carries ``disabled_reason`` (``None`` when active) so ``account list
    --include-disabled`` surfaces the operator-supplied reason without a
    per-account ``account view`` probe (§V.118, mirror of `CompanySummary`).
    """

    id: str
    email: str
    display_name: str
    last_synced_at: datetime | None
    disabled_reason: str | None = None
    created_at: datetime


class CompanyProfile(BaseModel):
    """Cold-email-grade lead profile distilled from web sources per §V.72.

    Validated by ``database.update_company`` before persistence to the
    ``company.profile JSONB`` column. Required fields {summary, products,
    target_customers, sources} must be non-empty for a profile to be
    considered "full"; optional ``timezone`` is null on multi-zone or
    unclear cases.
    """

    summary: str
    products: list[str]
    target_customers: str
    timezone: str | None = None
    sources: list[str]


class Company(BaseModel):
    """Target company for outbound campaigns."""

    id: str
    name: str
    domain: str
    profile: dict[str, Any] | None = None
    disabled_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class CompanySummary(BaseModel):
    """List-view projection of `Company`.

    Carries ``contact_count`` (child COUNT via LEFT JOIN contact, **including
    disabled** rows per §V.96) so ``company list --max-contacts`` /
    ``--min-contacts`` filter on the child cardinality without a per-company
    N+1 probe; counting disabled rows keeps it aligned with the
    discovery-memoization rule (§V.96), not the active-only set.
    """

    id: str
    name: str
    domain: str
    has_profile: bool
    contact_count: int
    disabled_reason: str | None = None
    created_at: datetime


class Contact(BaseModel):
    """Individual contact linked to a company.

    ``disabled_reason`` is the single status surface (§T.47): ``None`` means
    active, any non-NULL string means the contact is globally blocked and
    carries the human-readable reason (e.g. ``"bounced: hard bounce"``,
    ``"unsubscribed: replied 2026-05-14"``).

    ``title`` (role label) and ``email_confidence`` are flat lead-metadata
    columns per §V.95; ``email_confidence`` is the sole email-risk score
    (0-100, low = high risk), ``None`` when Bouncer has no signal.
    """

    id: str
    email: str
    company_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    email_confidence: int | None = None
    disabled_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class ContactSummary(BaseModel):
    """List-view projection of `Contact`.

    Carries ``email_confidence`` so ``contact list --max-email-confidence``
    surfaces the deliverability score alongside the row it filters on (§V.95).
    Carries ``title`` + ``company_domain`` (joined via LEFT JOIN company per
    §V.5) so the operator reads role + org from the CLI without a separate
    company lookup; ``company_domain`` is NULL when ``company_id`` is NULL.
    """

    id: str
    email: str
    first_name: str | None
    last_name: str | None
    title: str | None
    company_id: str | None
    company_domain: str | None
    email_confidence: int | None
    disabled_reason: str | None
    created_at: datetime


WorkflowType = Literal["inbound", "outbound"]
WorkflowStatus = Literal["draft", "active", "paused"]
WorkflowTemplateName = Literal[
    "outbound-general",
    "inbound-general",
    "inbound-google-drive",
]


class Workflow(BaseModel):
    """Workflow binding an account to instructions and a direction."""

    id: str
    name: str
    template: WorkflowTemplateName
    type: WorkflowType
    account_id: str
    account_email: str
    status: WorkflowStatus = "draft"
    objective: str = ""
    instructions: str = ""
    theme: str = "blue"
    created_at: datetime
    updated_at: datetime


class WorkflowSummary(BaseModel):
    """List-view projection of `Workflow`."""

    id: str
    name: str
    template: WorkflowTemplateName
    type: WorkflowType
    account_id: str
    account_email: str
    status: WorkflowStatus
    created_at: datetime


class WorkflowTemplateSummary(BaseModel):
    """List-view projection of a workflow template (code-defined, read-only)."""

    name: WorkflowTemplateName
    direction: WorkflowType
    description: str
    tool_count: int


class WorkflowTemplateRecord(BaseModel):
    """Full read-only record of a workflow template for CLI `template view`."""

    name: WorkflowTemplateName
    direction: WorkflowType
    description: str
    tools: list[str]
    protocol: str


EnrollmentStatus = Literal["active", "disabled"]


class Enrollment(BaseModel):
    """A contact's binding to a workflow.

    Status is the single live-state signal, collapsed to two values (§V.15):
    ``active`` (agent considers this contact when the workflow runs) is the
    only running state, and ``disabled`` is the operator halt -- reversible via
    ``enrollment enable``. Outcomes (completed/failed) live in the activity
    timeline, not in this row.

    ``disabled_reason`` is coupled to ``status='disabled'`` at the schema
    level: disabled rows always carry a non-empty reason; non-disabled rows
    carry NULL.

    ``workflow_name``, ``contact_email``, ``contact_name`` are
    denormalised parent identifiers loaded via JOIN at fetch (§V.5 parent-NI
    rule, ``Workflow.account_email`` precedent). They keep every CLI surface
    (``enrollment add/view/list/disable/enable/run``) symmetric on parent
    context.
    """

    id: str
    workflow_id: str
    workflow_name: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus = "active"
    reason: str = ""
    disabled_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class EnrollmentSummary(BaseModel):
    """List-view projection of `Enrollment` joined with workflow and contact."""

    id: str
    workflow_id: str
    workflow_name: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus
    updated_at: datetime


EnrollmentOutcome = Literal["completed", "failed"]


class EnrollmentWithOutcome(BaseModel):
    """Enrollment plus the latest outcome activity, if any.

    Outcomes (`completed` / `failed`) are timeline-only per §V.15 -- they do
    not live on the enrollment row. This composite carries the most recent
    `enrollment_completed` / `enrollment_failed` activity so the agent can
    coordinate across contacts in a single read.
    """

    id: str
    workflow_id: str
    contact_id: str
    status: EnrollmentStatus
    reason: str
    created_at: datetime
    updated_at: datetime
    latest_outcome: EnrollmentOutcome | None = None
    latest_outcome_reason: str | None = None
    latest_outcome_at: datetime | None = None


EmailDirection = Literal["inbound", "outbound"]


class Email(BaseModel):
    """Email message (inbound or outbound)."""

    id: str
    gmail_message_id: str | None = None
    gmail_thread_id: str | None = None
    rfc2822_message_id: str | None = None
    in_reply_to: str | None = None
    references_header: str | None = None
    account_id: str
    contact_id: str | None = None
    workflow_id: str | None = None
    direction: EmailDirection
    subject: str = ""
    body_text: str = ""
    labels: list[str] = []
    status: str = "received"
    is_routed: bool = False
    route_method: str | None = None
    sender: str = ""
    recipients: dict[str, list[str]] = {}
    sent_at: datetime | None = None
    received_at: datetime | None = None
    created_at: datetime


class EmailSummary(BaseModel):
    """List-view projection of `Email`."""

    id: str
    account_id: str
    contact_id: str | None
    workflow_id: str | None
    direction: EmailDirection
    subject: str
    sender: str
    status: str
    is_routed: bool
    route_method: str | None
    gmail_thread_id: str | None
    sent_at: datetime | None
    received_at: datetime | None


TaskStatus = Literal["pending", "completed", "failed", "cancelled"]


class Task(BaseModel):
    """Deferred agent work with scheduled execution."""

    id: str
    enrollment_id: str
    workflow_id: str
    contact_id: str
    email_id: str | None = None
    description: str
    context: dict[str, object] = {}
    scheduled_at: datetime
    status: TaskStatus = "pending"
    result: dict[str, object] = {}
    attempt_count: int = 0
    completed_at: datetime | None = None
    created_at: datetime


class TaskSummary(BaseModel):
    """List-view projection of `Task`."""

    id: str
    enrollment_id: str
    workflow_id: str
    contact_id: str
    email_id: str | None
    description: str
    scheduled_at: datetime
    status: TaskStatus
    attempt_count: int = 0


ActivityType = Literal[
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
    "enrollment_disabled",
    "enrollment_enabled",
]


class Activity(BaseModel):
    """Chronological event in a contact or company timeline.

    Either ``contact_id`` or ``company_id`` must be set (or both, for
    contact events that should also surface in the company timeline).
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``,
    ``enrollment_id``) let reports join activity to source records without
    parsing ``detail`` JSON.
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    email_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    enrollment_id: str | None = None
    type: ActivityType
    summary: str = ""
    detail: dict[str, object] = {}
    created_at: datetime


class ActivitySummary(BaseModel):
    """List-view projection of `Activity`."""

    id: str
    contact_id: str | None
    company_id: str | None
    email_id: str | None
    workflow_id: str | None
    task_id: str | None
    enrollment_id: str | None
    type: ActivityType
    summary: str
    created_at: datetime


class Tag(BaseModel):
    """A defined tag in the operator-maintained controlled vocabulary (§V.116).

    One row per defined tag, ``name`` globally unique (§V.90). A tag is a
    vocabulary entry, not a per-owner label -- owners are linked to it via
    ``TagAssignment``. ``disabled_reason`` non-null soft-retires the vocabulary
    entry (§V.10); a retired tag stays linked to its owners but drops out of the
    default ``tag list``.
    """

    id: str
    name: str
    disabled_reason: str | None = None
    created_at: datetime


class TagSummary(BaseModel):
    """List/view projection of `Tag` carrying ``usage_count`` (§V.116).

    ``usage_count`` is the number of ``tag_assignment`` rows pointing at the
    vocabulary entry (how many owners carry the tag), projected by a join so
    ``tag list`` and ``tag view`` report usage without an N+1 probe.
    """

    id: str
    name: str
    usage_count: int
    disabled_reason: str | None = None
    created_at: datetime


class TagAssignment(BaseModel):
    """A link binding a vocabulary `Tag` to one owner (§V.116).

    Exactly one of ``contact_id`` or ``company_id`` is set (XOR enforced at the
    schema level, mirroring §V.13). The link is created by ``tag add`` and
    deleted by ``tag remove``; it carries no disabled state of its own -- the
    soft-disable lifecycle lives on the vocabulary `Tag`.
    """

    id: str
    tag_id: str
    contact_id: str | None = None
    company_id: str | None = None
    created_at: datetime


class Note(BaseModel):
    """Freeform text annotation on a contact or company.

    Exactly one of ``contact_id`` or ``company_id`` is set (XOR enforced
    at the schema level).
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    body: str
    created_at: datetime


class NoteSummary(BaseModel):
    """List-view projection of `Note` with truncated body preview."""

    id: str
    contact_id: str | None
    company_id: str | None
    body_preview: str
    created_at: datetime


class ContactView(BaseModel):
    """View-only projection of `Contact` with inlined notes (§V.8).

    Used by both CLI ``contact view`` and agent tool ``read_contact`` so the
    operator and the agent see byte-identical context. ``notes`` carries the
    contact's own notes (full body, ORDER BY ``created_at`` DESC, capped at
    ``_INLINE_NOTES_CAP`` in ``database.py``); ``company_notes`` carries the
    parent company's notes when ``company_id`` is set, else an empty list.
    Totals reflect the actual row count in the database, not the cap.

    Per §V.8 the projection is a base-entity superset: it carries every
    ``Contact`` column (including the ``title`` + ``email_confidence`` lead
    metadata per §V.95) plus ``company_domain`` (LEFT JOIN company per §V.5,
    NULL when ``company_id`` is NULL), so ``contact view`` carries every
    ``contact list`` field and the singular ``{"contact": {...}}`` field set
    stays verb-invariant. Omitting a base column would let Pydantic
    ``extra=ignore`` silently strip it from ``**contact.model_dump()`` (§B.94).
    """

    id: str
    email: str
    company_id: str | None = None
    company_domain: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    email_confidence: int | None = None
    disabled_reason: str | None = None
    notes: list[Note] = []
    notes_total: int = 0
    company_notes: list[Note] = []
    company_notes_total: int = 0
    created_at: datetime
    updated_at: datetime


class CompanyView(BaseModel):
    """View-only projection of `Company` with inlined notes (§V.8).

    Used by both CLI ``company view`` and agent tool ``read_company``. Only
    the company's own notes are inlined (capped, full body, DESC); company is
    a root entity with no parent to inherit from.
    """

    id: str
    name: str
    domain: str
    profile: dict[str, Any] | None = None
    disabled_reason: str | None = None
    notes: list[Note] = []
    notes_total: int = 0
    created_at: datetime
    updated_at: datetime


class SyncStatus(BaseModel):
    """Singleton row tracking the running sync process."""

    id: str = "singleton"
    pid: int
    started_at: datetime
    heartbeat_at: datetime


class SchemaMetadata(BaseModel):
    """Singleton row recording version + normalized schema hash.

    Written at schema-apply time per §V.18.
    """

    mailpilot_version: str
    schema_hash: str
    applied_at: datetime


SchemaVerdict = Literal["current", "pending", "drift"]


class SchemaStatus(BaseModel):
    """Three-state schema verdict + supporting facts (§V.109).

    Computed by ``database.determine_schema_verdict``; consumed by the status
    ``schema`` block (§V.11), the ``run``/mutation write-gate (dead-stop on
    ``pending``/``drift``), and the read-only ``db check`` report. ``verdict``
    breaks the old metadata-row-missing vs table-missing collapse: a ledger
    behind the shipped migrations reads ``pending`` (run ``db migrate``), a
    hash mismatch with no migration path reads ``drift`` (investigate).
    """

    verdict: SchemaVerdict
    recorded_hash: str | None
    current_hash: str
    applied: int
    pending: int
