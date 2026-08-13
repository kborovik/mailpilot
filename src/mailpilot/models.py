"""Shared domain models mirroring schema.sql tables."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_serializer


class AccountSignature(BaseModel):
    """Nested account email signature projection (§V.151).

    CLI list|view|create|update emit this under ``signature`` (or ``null``
    when every field is empty). Flat DB columns stay on ``Account`` /
    ``AccountSummary`` for SQL row mapping.
    """

    full_name: str | None = None
    title: str | None = None
    website: str | None = None
    phone: str | None = None


def _normalize_signature_field(value: str | None) -> str | None:
    """Collapse empty strings to ``None`` for nested projection."""
    if value is None or value == "":
        return None
    return value


def signature_from_fields(
    full_name: str | None = None,
    title: str | None = None,
    website: str | None = None,
    phone: str | None = None,
) -> AccountSignature | None:
    """Build nested signature or ``None`` when all fields are empty (§V.151)."""
    fn = _normalize_signature_field(full_name)
    ti = _normalize_signature_field(title)
    we = _normalize_signature_field(website)
    ph = _normalize_signature_field(phone)
    if fn is None and ti is None and we is None and ph is None:
        return None
    return AccountSignature(full_name=fn, title=ti, website=we, phone=ph)


def _serialize_with_nested_signature(
    data: dict[str, Any],
) -> dict[str, Any]:
    """Replace flat signature_* keys with nested ``signature`` (§V.151)."""
    sig = signature_from_fields(
        full_name=data.pop("signature_full_name", None),
        title=data.pop("signature_title", None),
        website=data.pop("signature_website", None),
        phone=data.pop("signature_phone", None),
    )
    data["signature"] = sig.model_dump(mode="json") if sig is not None else None
    return data


class Account(BaseModel):
    """Gmail account managed by MailPilot."""

    id: str
    email: str
    display_name: str = ""
    gmail_history_id: str | None = None
    watch_expiration: datetime | None = None
    last_synced_at: datetime | None = None
    disabled_reason: str | None = None
    signature_full_name: str | None = None
    signature_title: str | None = None
    signature_website: str | None = None
    signature_phone: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_serializer(mode="wrap")
    def _serialize(
        self, handler: Callable[[Account], dict[str, Any]]
    ) -> dict[str, Any]:
        return _serialize_with_nested_signature(handler(self))

    def account_signature(self) -> AccountSignature | None:
        """Nested signature for harness render (§V.151)."""
        return signature_from_fields(
            self.signature_full_name,
            self.signature_title,
            self.signature_website,
            self.signature_phone,
        )


class AccountSummary(BaseModel):
    """List-view projection of `Account`.

    Carries ``disabled_reason`` (``None`` when active) so ``account list
    --include-disabled`` surfaces the operator-supplied reason without a
    per-account ``account view`` probe (§V.118, mirror of `CompanySummary`).

    Nested ``signature`` projected the same as full Account (§V.151).
    """

    id: str
    email: str
    display_name: str
    last_synced_at: datetime | None
    disabled_reason: str | None = None
    signature_full_name: str | None = None
    signature_title: str | None = None
    signature_website: str | None = None
    signature_phone: str | None = None
    created_at: datetime

    @model_serializer(mode="wrap")
    def _serialize(
        self, handler: Callable[[AccountSummary], dict[str, Any]]
    ) -> dict[str, Any]:
        return _serialize_with_nested_signature(handler(self))


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

    ``tags`` is the assigned tag-name list (empty ok; same shape as
    ``db export`` company.tags and ``CompanyView.tags``, §V.8 / §V.116).
    ``disabled_reason`` is always projected (null when enabled; value when
    the row is returned via ``--include-disabled``, §V.114). ``profile`` is
    None on the default lean list; ``company list --full`` embeds only
    ``{"summary": ...}`` (null when the company has no profile) so triage
    does not require N ``company view`` calls.
    """

    id: str
    name: str
    domain: str
    has_profile: bool
    contact_count: int
    tags: list[str] = []
    disabled_reason: str | None = None
    profile: dict[str, Any] | None = None
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

    ``verification_meta`` is operator-only durable verification audit (§V.144)
    (e.g. Bouncer status, source). It is never part of the agent prompt
    allowlist; default ``ContactView`` / ``load_contact_view`` omit it.
    """

    id: str
    email: str
    company_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    email_confidence: int | None = None
    verification_meta: dict[str, Any] | None = None
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


class TouchMessage(BaseModel):
    """Structured output of a compose-only outbound touch (§V.136).

    A touch run (first reach-out or a system-scheduled follow-up in a
    workflow's cadence) returns this instead of driving a tool loop: the
    validated output *is* the action, so the compose-only agent binds zero
    tools (§V.81 exempt). ``subject`` is the new-thread subject on the first
    touch and ``None`` on a later follow-up that continues an existing thread
    (the harness threads the reply and reuses the thread's subject); ``body``
    is the plain-text message. The harness sends it via ``email_ops`` and
    schedules the next touch -- one LLM call per touch, the send structural
    (§V.120, §V.136).
    """

    subject: str | None = None
    body: str


WorkflowType = Literal["inbound", "outbound"]
WorkflowStatus = Literal["draft", "active", "paused"]
WorkflowTemplateName = Literal[
    "outbound-general",
    "inbound-general",
    "inbound-google-drive",
]


class Workflow(BaseModel):
    """Workflow binding an account to instructions and a direction.

    ``touches`` and ``touch_interval_days`` are the system-owned cadence def
    fields (§V.136): ``touches`` is the total number of sends in the sequence
    and ``touch_interval_days`` is the spacing between them. They form a
    nullable pair -- both ``None`` means single-touch (no automatic follow-up);
    the schema CHECK forbids setting one without the other. Like the other def
    fields they are import-only (§V.103) and covered by the ``workflow check``
    wording hash (§V.134).
    """

    id: str
    name: str
    template: WorkflowTemplateName
    type: WorkflowType
    account_id: str
    account_email: str
    status: WorkflowStatus = "draft"
    goal: str = ""
    instructions: str = ""
    theme: str = "blue"
    touches: int | None = None
    touch_interval_days: int | None = None
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


class TouchStageCounts(BaseModel):
    """Per-touch sent/pending counts inside ``WorkflowStats.touches`` (§V.132)."""

    sent: int = 0
    pending: int = 0


class WorkflowStats(BaseModel):
    """Per-campaign funnel for one workflow at enrollment grain (§V.132).

    Eight stage counts computed by a single deterministic SQL aggregate over
    the workflow's enrollments -- no LLM. The enrollment row (one per contact)
    is the grain, so every count is contact-distinct: a multi-touch outbound
    sequence never double-counts.

    Touch-level slices (``touches``, ``awaiting_first_touch``, ``disabled``)
    are additive execution fields for multi-touch cadence triage.

    ``workflow_id`` and ``workflow_name`` carry the parent identity (§V.5) so
    the CLI envelope names the campaign without a separate lookup. The envelope
    key is ``workflow_stats`` (an aggregate, not a ``workflow`` entity row) per
    §V.132.
    """

    workflow_id: str
    workflow_name: str
    enrolled: int
    sent: int
    bounced: int
    replied: int
    meeting_booked: int
    contact_later: int
    do_not_contact: int
    active: int
    touches: dict[str, TouchStageCounts] = Field(default_factory=dict)
    awaiting_first_touch: int = 0
    disabled: int = 0


WorkflowCheckState = Literal["in_sync", "out_of_sync", "not_imported", "orphaned"]


class WorkflowCheckEntry(BaseModel):
    """One workflow name's wording-integrity state (§V.134).

    The state names how the catalog def and the live row line up for one
    globally unique ``name`` (§V.90), the join key (never a hashed field):

    - ``in_sync``: name on both sides, wording hashes equal.
    - ``out_of_sync``: name on both sides, hashes differ (re-import due).
    - ``not_imported``: name in a catalog def, no matching row.
    - ``orphaned``: name in a row, no matching catalog def.

    ``catalog_hash`` is ``None`` when orphaned (no def) and ``row_hash`` is
    ``None`` when not imported (no row); both are SHA-256 over the def fields
    ``{template, theme, goal, instructions, touches, touch_interval_days}`` (the
    cadence pair joined the hashed set per §V.136).
    """

    name: str
    state: WorkflowCheckState
    catalog_hash: str | None
    row_hash: str | None


class WorkflowCheck(BaseModel):
    """Aggregate wording-integrity report over the checked workflow names (§V.134).

    A read-only 2-way live SHA-256 comparison mirroring ``db check``: each
    ``workflows/*.toml`` def is joined to the live rows by ``name`` and
    classified into one of four states. A directory check spans every name on
    either side; a specific-file check reports only the passed names, so
    ``orphaned`` (a row with no def) appears in directory mode only (§V.134).
    ``ok:true`` is reported regardless of state -- the check informs, it is
    never a deploy gate. The envelope key is ``workflow_check`` (an aggregate,
    not a ``workflow`` entity row, cf §V.132).
    """

    workflows: list[WorkflowCheckEntry]
    in_sync: int
    out_of_sync: int
    not_imported: int
    orphaned: int


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
    """List-view projection of `Enrollment` joined with workflow and contact.

    Lean fields are always populated. Execution fields (company, touch progress,
    next send, disposition, created_at) are populated only when the caller
    requests ``full=True`` per §V.152; lean dumps exclude them so agent payloads
    stay small.
    """

    id: str
    workflow_id: str
    workflow_name: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus
    updated_at: datetime
    # §V.152 --full denser projection (None when lean)
    company_domain: str | None = None
    company_name: str | None = None
    emails_sent: int | None = None
    last_touch: int | None = None
    next_scheduled_at: datetime | None = None
    next_touch: int | None = None
    disposition: str | None = None
    created_at: datetime | None = None


# Fields present only on ``--full`` enrollment list/view projections (§V.152).
ENROLLMENT_FULL_FIELDS: frozenset[str] = frozenset(
    {
        "company_domain",
        "company_name",
        "emails_sent",
        "last_touch",
        "next_scheduled_at",
        "next_touch",
        "disposition",
        "created_at",
    }
)


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


class EnrollmentPreviewContact(BaseModel):
    """One candidate contact in a tag-cohort enrollment dry-run (§V.150)."""

    email: str
    title: str | None = None
    company_domain: str | None = None
    company_tags: list[str] = Field(default_factory=list)
    contact_tags: list[str] = Field(default_factory=list)
    email_confidence: int | None = None
    peer_workflows: list[str] = Field(default_factory=list)


class EnrollmentPreviewExcluded(BaseModel):
    """Drop counters for a tag-cohort enrollment dry-run (§V.150)."""

    disabled_companies: int = 0
    already_enrolled: int = 0
    self_loop: int = 0
    disabled_contacts: int = 0


class EnrollmentPreview(BaseModel):
    """Read-only enrollment dry-run report for a company-or-contact tag (§V.150).

    No rows are written. ``count`` equals ``len(contacts)`` and is the
    ``record_count`` the CLI envelope reports. ``--tag`` is a union of
    company-tag and contact-tag owners, deduped by contact id.
    """

    workflow: str
    tag: str
    count: int
    contacts: list[EnrollmentPreviewContact]
    excluded: EnrollmentPreviewExcluded


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
    recipients: dict[str, list[str]]
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


class TaskStats(BaseModel):
    """Task-cadence aggregate over the task queue at task grain (§V.133).

    Per-status counts plus ``total``, the distinct scheduled-day count bucketed
    in a chosen IANA timezone, and the first/last ``scheduled_at`` timestamps.
    Computed by a single deterministic SQL aggregate -- no LLM. Optional
    ``--workflow-id`` (§V.107) and ``--trigger`` (§V.26 taxonomy) filters narrow
    the task set before aggregation. The envelope key is ``task_stats`` (an
    aggregate, not a ``task`` entity row) per §V.133 -- mirrors the
    ``workflow_stats`` exception of §V.132.

    ``first_scheduled_at`` / ``last_scheduled_at`` are ``None`` when the filtered
    task set is empty (``MIN`` / ``MAX`` over zero rows).
    """

    total: int
    pending: int
    completed: int
    failed: int
    cancelled: int
    distinct_scheduled_days: int
    first_scheduled_at: datetime | None
    last_scheduled_at: datetime | None


class WorkflowReportMeta(BaseModel):
    """Workflow identity + cadence for ``workflow report`` (§V.153)."""

    name: str
    touches: int | None = None
    touch_interval_days: int | None = None
    status: WorkflowStatus


class WorkflowReport(BaseModel):
    """Composite campaign report: funnel + tasks + enrollment matrix (§V.153)."""

    workflow: WorkflowReportMeta
    funnel: WorkflowStats
    tasks: TaskStats
    enrollments: list[EnrollmentSummary]


class WorkflowStatusHealth(BaseModel):
    """Ops-health composite for ``workflow status`` (§V.157), not funnel."""

    workflow: WorkflowReportMeta
    wording: str  # in_sync | out_of_sync | not_imported | orphaned | unknown
    run_loop: str  # ok | stale | stopped
    overdue_tasks: int
    failed_tasks_24h: int
    enrollments_never_sent: int
    funnel_active: int | None = None


QueueGrain = Literal["workflow", "task"]


class QueueWorkflowRow(BaseModel):
    """One workflow-grain row for ``show queue`` (§V.166)."""

    workflow_name: str
    status: WorkflowStatus
    active: int
    pending: int
    overdue: int
    due_today: int
    next_at: datetime | None
    failed_24h: int
    never_sent: int


class QueueTaskRow(BaseModel):
    """One pending-task row for ``show queue --detail`` (§V.166).

    Table render hides ``task_id``, ``enrollment_id``, and ``scheduled_at``.
    JSON keeps those plus relative ``when``.
    """

    when: str
    scheduled_at: datetime
    contact: str
    email: str
    company: str = ""
    workflow_name: str
    touch: str = ""
    trigger: str = ""
    state: str
    attempts: int
    task_id: str
    enrollment_id: str


class QueueReport(BaseModel):
    """Operator queue report for ``show queue`` (§V.166).

    Envelope key ``queue``. ``record_count`` is ``len(rows)``, not 1.
    """

    grain: QueueGrain
    tz: str
    rows: list[QueueWorkflowRow] | list[QueueTaskRow]


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


MeetingStatus = Literal["scheduled", "completed", "cancelled", "no_show"]


class Meeting(BaseModel):
    """A calendar meeting, first-class entity peer to email (§V.125).

    One row per Google Calendar event, keyed on ``google_event_id``
    (nullable-unique, idempotent ingest, mirrors ``email.gmail_message_id``
    §V.90). Attendees link through ``MeetingAttendee``. ``status`` is operator
    record-keeping only and gates nothing -- booking conclusion (§V.128) fires
    at booking regardless of a later ``completed``/``no_show`` (§V.125).
    """

    id: str
    google_event_id: str | None = None
    meet_url: str | None = None
    summary: str = ""
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None
    status: MeetingStatus = "scheduled"
    created_at: datetime
    updated_at: datetime


class MeetingAttendee(BaseModel):
    """A link binding a `Meeting` to one attendee contact (§V.125).

    UNIQUE per ``(meeting_id, contact_id)`` pair (mirrors `TagAssignment`
    §V.116). One meeting links one or more attendees; attendees are matched to
    contacts by email at ingest time, an unmatched email produces no link.
    """

    id: str
    meeting_id: str
    contact_id: str
    created_at: datetime


class MeetingSummary(BaseModel):
    """List-view projection of `Meeting` with a compact attendee summary (§V.8).

    Carries ``attendee_emails`` + ``attendee_count`` (child-aggregate denorm
    over ``meeting_attendee`` joined to ``contact``, mirroring ``contact_count``
    §V.96) so a ``meeting list --contact-email`` result names who attends
    without a per-row attendee probe. The reader half of the link relation whose
    writer is ``link_meeting_attendee`` and whose filter is ``--contact-email``
    (§B.112).
    """

    id: str
    google_event_id: str | None = None
    meet_url: str | None = None
    summary: str = ""
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None
    status: MeetingStatus = "scheduled"
    attendee_emails: list[str] = []
    attendee_count: int = 0
    created_at: datetime


class MeetingView(BaseModel):
    """View-only projection of `Meeting` with inlined attendees (§V.8).

    Used by CLI ``meeting view``. ``attendees`` carries the meeting's full
    attendee `Contact` rows (email + name + every base column) joined via
    ``meeting_attendee`` (§V.125), so the operator sees who attends -- the
    reader for the write+filter relation that previously had none (§B.112).

    Per §V.8 the projection is a base-entity superset: it carries every
    ``Meeting`` column (forwarded via ``**meeting.model_dump()``) plus the
    ``MeetingSummary`` attendee denorm (``attendee_emails`` + ``attendee_count``)
    and the inlined ``attendees`` list. Omitting a base column would let
    Pydantic ``extra=ignore`` silently strip it (§B.94).
    """

    id: str
    google_event_id: str | None = None
    meet_url: str | None = None
    summary: str = ""
    scheduled_at: datetime | None = None
    ends_at: datetime | None = None
    status: MeetingStatus = "scheduled"
    attendees: list[Contact] = []
    attendee_emails: list[str] = []
    attendee_count: int = 0
    created_at: datetime
    updated_at: datetime


class ContactView(BaseModel):
    """View-only projection of `Contact` with inlined notes (§V.8).

    Used by CLI ``contact view`` (default) and the workflow-agent prompt
    pre-feed (``Contact record:`` section, §V.135) so the operator and the
    agent see byte-identical context. ``notes`` carries the contact's own
    notes (full body, ORDER BY ``created_at`` DESC, capped at
    ``_INLINE_NOTES_CAP`` in ``database.py``); ``company_notes`` carries the
    parent company's notes when ``company_id`` is set, else an empty list.
    Totals reflect the actual row count in the database, not the cap.

    Per §V.8 the projection is a base-entity superset of agent-facing
    columns: every ``Contact`` column except operator-only
    ``verification_meta`` (§V.144; opt-in via ``contact view --include-meta``)
    plus ``company_domain`` (LEFT JOIN company per §V.5, NULL when
    ``company_id`` is NULL). Omitting an agent-facing base column would let
    Pydantic ``extra=ignore`` silently strip it from ``**contact.model_dump()``
    (§B.94).
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

    Used by CLI ``company view`` and the workflow-agent prompt pre-feed
    (``Company record:`` section, §V.135). Only
    the company's own notes are inlined (capped, full body, DESC); company is
    a root entity with no parent to inherit from.

    ``tags`` is the assigned tag-name list (empty ok; same shape as
    ``CompanySummary.tags`` / ``db export`` company.tags, §V.116).
    ``aliases`` is the sorted lowercased alternate-domain list (empty ok;
    view-only — lean list omits, §V.142).
    """

    id: str
    name: str
    domain: str
    profile: dict[str, Any] | None = None
    tags: list[str] = []
    aliases: list[str] = []
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
