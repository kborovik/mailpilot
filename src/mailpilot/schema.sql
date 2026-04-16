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

CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL REFERENCES account(id),
    type              TEXT NOT NULL CHECK (type IN ('inbound', 'outbound')),
    name              TEXT NOT NULL,
    objective         TEXT NOT NULL DEFAULT '',
    instructions      TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'active', 'paused')),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS workflow_contact (
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'active', 'completed', 'failed')),
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, contact_id)
);

CREATE TABLE IF NOT EXISTS email (
    id                TEXT PRIMARY KEY,
    gmail_message_id  TEXT UNIQUE,
    gmail_thread_id   TEXT,
    account_id        TEXT NOT NULL REFERENCES account(id),
    contact_id        TEXT REFERENCES contact(id),
    workflow_id       TEXT REFERENCES workflow(id),
    direction         TEXT NOT NULL CHECK (direction IN ('inbound', 'outbound')),
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
    completed_at  TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_status (
    id            TEXT PRIMARY KEY DEFAULT 'singleton',
    pid           INTEGER NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_name ON company(LOWER(name));
CREATE INDEX IF NOT EXISTS idx_contact_domain ON contact(domain);
CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contact(company_id);
CREATE INDEX IF NOT EXISTS idx_workflow_account_id ON workflow(account_id);
CREATE INDEX IF NOT EXISTS idx_workflow_contact_contact_id ON workflow_contact(contact_id);
CREATE INDEX IF NOT EXISTS idx_task_workflow_id ON task(workflow_id);
CREATE INDEX IF NOT EXISTS idx_task_contact_id ON task(contact_id);
CREATE INDEX IF NOT EXISTS idx_task_scheduled_at ON task(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_email_account_id ON email(account_id);
CREATE INDEX IF NOT EXISTS idx_email_contact_id ON email(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_workflow_id ON email(workflow_id);
CREATE INDEX IF NOT EXISTS idx_email_gmail_thread_id ON email(gmail_thread_id);
