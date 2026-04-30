"""Shared domain models mirroring schema.sql tables."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class Account(BaseModel):
    """Gmail account managed by MailPilot."""

    id: str
    email: str
    display_name: str = ""
    gmail_history_id: str | None = None
    watch_expiration: datetime | None = None
    last_synced_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AccountSummary(BaseModel):
    """List-view projection of `Account`."""

    id: str
    email: str
    display_name: str
    last_synced_at: datetime | None
    created_at: datetime


class Company(BaseModel):
    """Target company for outbound campaigns."""

    id: str
    name: str
    domain: str
    domain_aliases: list[str] = []
    profile_summary: str | None = None
    linkedin: str | None = None
    industry: str | None = None
    products_services: list[str] = []
    employee_count: int | None = None
    founded_year: int | None = None
    locations: list[str] = []
    company_type: str | None = None
    recent_activity: str | None = None
    qualification_notes: str | None = None
    created_at: datetime
    updated_at: datetime


class CompanySummary(BaseModel):
    """List-view projection of `Company`."""

    id: str
    name: str
    domain: str
    industry: str | None
    employee_count: int | None
    created_at: datetime


class Contact(BaseModel):
    """Individual contact linked to a company."""

    id: str
    email: str
    domain: str
    company_id: str | None = None
    email_type: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    position: str | None = None
    seniority: str | None = None
    department: str | None = None
    profile_summary: str | None = None
    linkedin: str | None = None
    status: str = "active"
    status_reason: str = ""
    created_at: datetime
    updated_at: datetime


class ContactSummary(BaseModel):
    """List-view projection of `Contact`."""

    id: str
    email: str
    first_name: str | None
    last_name: str | None
    company_id: str | None
    status: str
    created_at: datetime


WorkflowType = Literal["inbound", "outbound"]
WorkflowStatus = Literal["draft", "active", "paused"]


class Workflow(BaseModel):
    """Workflow binding an account to instructions and a direction."""

    id: str
    name: str
    type: WorkflowType
    account_id: str
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
    type: WorkflowType
    account_id: str
    status: WorkflowStatus
    created_at: datetime


EnrollmentStatus = Literal["active", "paused"]


class Enrollment(BaseModel):
    """A contact's binding to a workflow.

    Status is operational state only -- ``active`` (agent considers this
    contact when the workflow runs) or ``paused`` (operator/agent has
    suspended). Outcomes (completed/failed) live in the activity timeline,
    not in this row.
    """

    workflow_id: str
    contact_id: str
    status: EnrollmentStatus = "active"
    reason: str = ""
    created_at: datetime
    updated_at: datetime


class EnrollmentSummary(BaseModel):
    """List-view projection of `Enrollment` joined with contact identity."""

    workflow_id: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus
    updated_at: datetime


EnrollmentOutcome = Literal["completed", "failed"]


class EnrollmentWithOutcome(BaseModel):
    """Enrollment plus the latest outcome activity, if any.

    Outcomes (`completed` / `failed`) are timeline-only per ADR-08 -- they do
    not live on the enrollment row. This composite carries the most recent
    `enrollment_completed` / `enrollment_failed` activity so the agent can
    coordinate across contacts in a single read.
    """

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
    sent_at: datetime | None
    received_at: datetime | None


TaskStatus = Literal["pending", "completed", "failed", "cancelled"]


class Task(BaseModel):
    """Deferred agent work with scheduled execution."""

    id: str
    workflow_id: str
    contact_id: str
    email_id: str | None = None
    description: str
    context: dict[str, object] = {}
    scheduled_at: datetime
    status: TaskStatus = "pending"
    result: dict[str, object] = {}
    completed_at: datetime | None = None
    created_at: datetime


class TaskSummary(BaseModel):
    """List-view projection of `Task`."""

    id: str
    workflow_id: str
    contact_id: str
    email_id: str | None
    description: str
    scheduled_at: datetime
    status: TaskStatus


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
]


class Activity(BaseModel):
    """Chronological event in a contact or company timeline.

    Either ``contact_id`` or ``company_id`` must be set (or both, for
    contact events that should also surface in the company timeline).
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``)
    let reports join activity to source records without parsing
    ``detail`` JSON.
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    email_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    type: ActivityType
    summary: str = ""
    detail: dict[str, object] = {}
    created_at: datetime


class ActivitySummary(BaseModel):
    """List-view projection of `Activity`."""

    id: str
    contact_id: str | None
    company_id: str | None
    type: ActivityType
    summary: str
    created_at: datetime


class Tag(BaseModel):
    """Flexible label on a contact or company for segmentation.

    Exactly one of ``contact_id`` or ``company_id`` is set (XOR enforced
    at the schema level).
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    name: str
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


class SyncStatus(BaseModel):
    """Singleton row tracking the running sync process."""

    id: str = "singleton"
    pid: int
    started_at: datetime
    heartbeat_at: datetime
