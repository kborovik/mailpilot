CREATE TABLE IF NOT EXISTS account (
    id                   TEXT PRIMARY KEY,
    email                TEXT UNIQUE NOT NULL,
    display_name         TEXT NOT NULL DEFAULT '',
    gmail_history_id     TEXT,
    watch_expiration     TIMESTAMPTZ,
    last_synced_at       TIMESTAMPTZ,
    disabled_reason      TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    domain                TEXT UNIQUE NOT NULL,
    profile               JSONB,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    disabled_reason       TEXT
);

CREATE INDEX IF NOT EXISTS idx_company_name ON company(LOWER(name));

CREATE TABLE IF NOT EXISTS contact (
    id                    TEXT PRIMARY KEY,
    email                 TEXT UNIQUE NOT NULL,
    company_id            TEXT REFERENCES company(id),
    first_name            TEXT,
    last_name             TEXT,
    title                 TEXT,
    email_confidence      INT CHECK (email_confidence BETWEEN 0 AND 100),
    disabled_reason       TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contact(company_id);
CREATE INDEX IF NOT EXISTS idx_contact_active ON contact(id) WHERE disabled_reason IS NULL;

CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES account(id),
    template          TEXT NOT NULL
                      CHECK (template IN (
                          'outbound-general',
                          'inbound-general',
                          'inbound-google-drive'
                      )),
    type              TEXT NOT NULL CHECK (type IN ('inbound', 'outbound')),
    name              TEXT NOT NULL
                      CHECK (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    goal              TEXT NOT NULL DEFAULT '',
    instructions      TEXT NOT NULL DEFAULT '',
    theme             TEXT NOT NULL DEFAULT 'blue',
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'paused')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    touches             INTEGER,
    touch_interval_days INTEGER,
    UNIQUE (name),
    CONSTRAINT workflow_touch_cadence_check CHECK (
        ((touches IS NULL) = (touch_interval_days IS NULL))
        AND (touches IS NULL OR touches > 0)
        AND (touch_interval_days IS NULL OR touch_interval_days > 0)
    )
);

CREATE INDEX IF NOT EXISTS idx_workflow_account_id ON workflow(account_id);

CREATE TABLE IF NOT EXISTS enrollment (
    id              TEXT PRIMARY KEY,
    workflow_id     TEXT NOT NULL REFERENCES workflow(id),
    contact_id      TEXT NOT NULL REFERENCES contact(id),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'disabled')),
    reason          TEXT NOT NULL DEFAULT '',
    disabled_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (workflow_id, contact_id),
    CHECK ((status = 'disabled') = (disabled_reason IS NOT NULL AND TRIM(disabled_reason) <> ''))
);

CREATE INDEX IF NOT EXISTS idx_enrollment_contact_id ON enrollment(contact_id);

CREATE TABLE IF NOT EXISTS email (
    id                TEXT PRIMARY KEY,
    gmail_message_id  TEXT UNIQUE,
    gmail_thread_id   TEXT,
    rfc2822_message_id TEXT,
    in_reply_to       TEXT,
    references_header TEXT,
    account_id        TEXT NOT NULL REFERENCES account(id),
    contact_id        TEXT REFERENCES contact(id),
    workflow_id       TEXT REFERENCES workflow(id),
    direction         TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
    sender            TEXT NOT NULL DEFAULT '',
    recipients        JSONB NOT NULL DEFAULT '{}',
    subject           TEXT NOT NULL DEFAULT '',
    body_text         TEXT NOT NULL DEFAULT '',
    labels            JSONB NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'received'
                      CHECK (status IN ('sent', 'received', 'bounced')),
    is_routed         BOOLEAN NOT NULL DEFAULT FALSE,
    route_method      TEXT
                      CHECK (route_method IS NULL OR route_method IN (
                          'thread_match',
                          'rfc_message_id_match',
                          'classified',
                          'skipped_outside_window',
                          'skipped_no_workflows',
                          'skipped_predates_workflows',
                          'skipped_no_inbound_workflows'
                      )),
    sent_at           TIMESTAMPTZ,
    received_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (route_method IS NULL OR is_routed = TRUE)
);

CREATE INDEX IF NOT EXISTS idx_email_account_id ON email(account_id);
CREATE INDEX IF NOT EXISTS idx_email_contact_id ON email(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_workflow_id ON email(workflow_id);
CREATE INDEX IF NOT EXISTS idx_email_gmail_thread_id ON email(gmail_thread_id);
CREATE INDEX IF NOT EXISTS idx_email_rfc2822_message_id ON email(rfc2822_message_id);

CREATE TABLE IF NOT EXISTS task (
    id             TEXT PRIMARY KEY,
    enrollment_id  TEXT NOT NULL REFERENCES enrollment(id),
    workflow_id    TEXT NOT NULL REFERENCES workflow(id),
    contact_id     TEXT NOT NULL REFERENCES contact(id),
    email_id       TEXT REFERENCES email(id),
    description    TEXT NOT NULL,
    context        JSONB NOT NULL DEFAULT '{}',
    scheduled_at   TIMESTAMPTZ NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    result         JSONB NOT NULL DEFAULT '{}',
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    completed_at   TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_workflow_id ON task(workflow_id);
CREATE INDEX IF NOT EXISTS idx_task_contact_id ON task(contact_id);
CREATE INDEX IF NOT EXISTS idx_task_scheduled_at ON task(scheduled_at) WHERE status = 'pending';

-- PG NOTIFY trigger: fires on every task INSERT and on UPDATEs that change
-- status or scheduled_at so the sync loop can drain the queue immediately
-- when a transient retry reschedule lands. Terminal-status updates also
-- wake the loop; the resulting empty drain is benign noise.
CREATE OR REPLACE FUNCTION notify_task_pending() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('task_pending', '');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_pending_trigger ON task;
CREATE TRIGGER task_pending_trigger
    AFTER INSERT OR UPDATE OF status, scheduled_at ON task
    FOR EACH ROW
    EXECUTE FUNCTION notify_task_pending();

CREATE TABLE IF NOT EXISTS activity (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    email_id        TEXT REFERENCES email(id),
    workflow_id     TEXT REFERENCES workflow(id),
    task_id         TEXT REFERENCES task(id),
    enrollment_id   TEXT REFERENCES enrollment(id),
    type            TEXT NOT NULL
                    CHECK (type IN (
                        'email_sent', 'email_received',
                        'note_added', 'tag_added', 'tag_removed',
                        'status_changed',
                        'enrollment_added',
                        'enrollment_completed', 'enrollment_failed',
                        'enrollment_paused', 'enrollment_resumed',
                        'enrollment_disabled', 'enrollment_enabled'
                    )),
    summary         TEXT NOT NULL DEFAULT '',
    detail          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (contact_id IS NOT NULL OR company_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_activity_contact_timeline
    ON activity(contact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_company_timeline
    ON activity(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type ON activity(type);

-- Tags = operator-maintained controlled vocabulary, two tables (§V.116).
-- `tag` holds the vocabulary (one row per defined tag, name globally unique
-- §V.90, soft-delete via disabled_reason §V.10). `tag_assignment` is the link
-- (one row per tag-and-owner pair, owner XOR contact|company mirroring §V.13).
CREATE TABLE IF NOT EXISTS tag (
    id              TEXT PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    disabled_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (disabled_reason IS NULL OR TRIM(disabled_reason) <> '')
);

CREATE TABLE IF NOT EXISTS tag_assignment (
    id              TEXT PRIMARY KEY,
    tag_id          TEXT NOT NULL REFERENCES tag(id),
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (contact_id IS NOT NULL AND company_id IS NULL)
        OR
        (contact_id IS NULL AND company_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_assignment_contact_unique
    ON tag_assignment(tag_id, contact_id)
    WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_assignment_company_unique
    ON tag_assignment(tag_id, company_id)
    WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tag_assignment_tag ON tag_assignment(tag_id);
CREATE INDEX IF NOT EXISTS idx_tag_assignment_contact
    ON tag_assignment(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tag_assignment_company
    ON tag_assignment(company_id) WHERE company_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS note (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (contact_id IS NOT NULL AND company_id IS NULL)
        OR
        (contact_id IS NULL AND company_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_note_contact_id ON note(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_note_company_id ON note(company_id) WHERE company_id IS NOT NULL;

-- Meeting = first-class entity peer to email (§V.125). One row per Google
-- Calendar event, keyed on `google_event_id` (nullable-unique, idempotent
-- ingest, mirrors email.gmail_message_id §V.90). `meeting_attendee` links one
-- meeting to >=1 contact (UNIQUE per pair, mirrors tag_assignment §V.116). The
-- `status` column is operator record-keeping only and gates nothing -- booking
-- conclusion (§V.128) fires at booking regardless of a later completed|no_show.
CREATE TABLE IF NOT EXISTS meeting (
    id              TEXT PRIMARY KEY,
    google_event_id TEXT UNIQUE,
    meet_url        TEXT,
    summary         TEXT NOT NULL DEFAULT '',
    scheduled_at    TIMESTAMPTZ,
    ends_at         TIMESTAMPTZ,
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled', 'completed', 'cancelled', 'no_show')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meeting_attendee (
    id              TEXT PRIMARY KEY,
    meeting_id      TEXT NOT NULL REFERENCES meeting(id),
    contact_id      TEXT NOT NULL REFERENCES contact(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (meeting_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_meeting_attendee_meeting ON meeting_attendee(meeting_id);
CREATE INDEX IF NOT EXISTS idx_meeting_attendee_contact ON meeting_attendee(contact_id);

CREATE TABLE IF NOT EXISTS sync_status (
    id            TEXT PRIMARY KEY DEFAULT 'singleton',
    pid           INTEGER NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS schema_metadata (
    id                 INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    mailpilot_version  TEXT NOT NULL,
    schema_hash        TEXT NOT NULL,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Forward-only migration ledger (§V.108). Non-singleton: one row per applied
-- migration keyed by monotonic version. Created here for fresh-DB builds; the
-- migrate machinery (database.migrate_database) ensures it exists on populated
-- DBs that predate the migration system, so its definition MUST stay in lockstep
-- with _ENSURE_MIGRATIONS_LEDGER_SQL (the init==migrations identity test guards
-- this).
CREATE TABLE IF NOT EXISTS schema_migrations (
    version            INTEGER PRIMARY KEY,
    name               TEXT NOT NULL,
    applied_at         TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    mailpilot_version  TEXT NOT NULL
);
