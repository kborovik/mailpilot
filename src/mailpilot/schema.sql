CREATE TABLE IF NOT EXISTS account (
    id                   TEXT PRIMARY KEY,
    email                TEXT UNIQUE NOT NULL,
    display_name         TEXT NOT NULL DEFAULT '',
    gmail_history_id     TEXT,
    watch_expiration     TIMESTAMPTZ,
    last_synced_at       TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company (
    id                    TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    domain                TEXT UNIQUE NOT NULL,
    domain_aliases        JSONB NOT NULL DEFAULT '[]',
    profile_summary       TEXT,
    linkedin              TEXT,
    industry              TEXT,
    products_services     JSONB NOT NULL DEFAULT '[]',
    employee_count        INTEGER,
    founded_year          INTEGER,
    locations             JSONB NOT NULL DEFAULT '[]',
    company_type          TEXT,
    recent_activity       TEXT,
    qualification_notes   TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_name ON company(LOWER(name));

CREATE TABLE IF NOT EXISTS contact (
    id                    TEXT PRIMARY KEY,
    email                 TEXT UNIQUE NOT NULL,
    domain                TEXT NOT NULL,
    company_id            TEXT REFERENCES company(id),
    email_type            TEXT,
    first_name            TEXT,
    last_name             TEXT,
    position              TEXT,
    seniority             TEXT,
    department            TEXT,
    profile_summary       TEXT,
    linkedin              TEXT,
    status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'bounced', 'unsubscribed')),
    status_reason         TEXT NOT NULL DEFAULT '',
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contact_domain ON contact(domain);
CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contact(company_id);

CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES account(id),
    type              TEXT NOT NULL CHECK (type IN ('inbound', 'outbound')),
    name              TEXT NOT NULL,
    objective         TEXT NOT NULL DEFAULT '',
    instructions      TEXT NOT NULL DEFAULT '',
    theme             TEXT NOT NULL DEFAULT 'blue',
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'paused')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, name)
);

CREATE INDEX IF NOT EXISTS idx_workflow_account_id ON workflow(account_id);

CREATE TABLE IF NOT EXISTS enrollment (
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused')),
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, contact_id)
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
    sent_at           TIMESTAMPTZ,
    received_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_email_account_id ON email(account_id);
CREATE INDEX IF NOT EXISTS idx_email_contact_id ON email(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_workflow_id ON email(workflow_id);
CREATE INDEX IF NOT EXISTS idx_email_gmail_thread_id ON email(gmail_thread_id);
CREATE INDEX IF NOT EXISTS idx_email_rfc2822_message_id ON email(rfc2822_message_id);

CREATE TABLE IF NOT EXISTS task (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    email_id      TEXT REFERENCES email(id),
    description   TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}',
    scheduled_at  TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'completed', 'failed', 'cancelled')),
    result        JSONB NOT NULL DEFAULT '{}',
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_task_workflow_id ON task(workflow_id);
CREATE INDEX IF NOT EXISTS idx_task_contact_id ON task(contact_id);
CREATE INDEX IF NOT EXISTS idx_task_scheduled_at ON task(scheduled_at) WHERE status = 'pending';

-- PG NOTIFY trigger: fires on every task INSERT so the sync loop can
-- drain the task queue immediately instead of waiting for the next poll.
CREATE OR REPLACE FUNCTION notify_task_pending() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('task_pending', '');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS task_pending_trigger ON task;
CREATE TRIGGER task_pending_trigger
    AFTER INSERT ON task
    FOR EACH ROW
    EXECUTE FUNCTION notify_task_pending();

CREATE TABLE IF NOT EXISTS activity (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    email_id        TEXT REFERENCES email(id),
    workflow_id     TEXT REFERENCES workflow(id),
    task_id         TEXT REFERENCES task(id),
    type            TEXT NOT NULL
                    CHECK (type IN (
                        'email_sent', 'email_received',
                        'note_added', 'tag_added', 'tag_removed',
                        'status_changed',
                        'enrollment_added',
                        'enrollment_completed', 'enrollment_failed',
                        'enrollment_paused', 'enrollment_resumed'
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

CREATE TABLE IF NOT EXISTS tag (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (contact_id IS NOT NULL AND company_id IS NULL)
        OR
        (contact_id IS NULL AND company_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_contact_unique
    ON tag(contact_id, name) WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_company_unique
    ON tag(company_id, name) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tag_name ON tag(name);

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

CREATE TABLE IF NOT EXISTS sync_status (
    id            TEXT PRIMARY KEY DEFAULT 'singleton',
    pid           INTEGER NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
