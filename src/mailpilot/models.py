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


EnrollmentStatus = Literal["pending", "active", "completed", "failed"]


class Enrollment(BaseModel):
    """A contact's participation in a workflow with lifecycle outcome."""

    workflow_id: str
    contact_id: str
    status: EnrollmentStatus = "pending"
    reason: str = ""
    created_at: datetime
    updated_at: datetime


class EnrollmentDetail(BaseModel):
    """Enrollment with denormalised contact info for list display."""

    workflow_id: str
    contact_id: str
    contact_email: str
    contact_name: str
    status: EnrollmentStatus
    reason: str
    created_at: datetime
    updated_at: datetime


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


ActivityType = Literal[
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "workflow_assigned",
    "workflow_completed",
    "workflow_failed",
]


class Activity(BaseModel):
    """Chronological event in a contact's relationship timeline."""

    id: str
    contact_id: str
    company_id: str | None = None
    type: ActivityType
    summary: str = ""
    detail: dict[str, object] = {}
    created_at: datetime


EntityType = Literal["contact", "company"]


class Tag(BaseModel):
    """Flexible label on a contact or company for segmentation."""

    id: str
    entity_type: EntityType
    entity_id: str
    name: str
    created_at: datetime


class Note(BaseModel):
    """Freeform text annotation on a contact or company."""

    id: str
    entity_type: EntityType
    entity_id: str
    body: str
    created_at: datetime


class SyncStatus(BaseModel):
    """Singleton row tracking the running sync process."""

    id: str = "singleton"
    pid: int
    started_at: datetime
    heartbeat_at: datetime
