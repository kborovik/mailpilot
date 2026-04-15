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
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    type              TEXT NOT NULL,
    account_id        TEXT NOT NULL REFERENCES account(id),
    status            TEXT NOT NULL DEFAULT 'draft',
    objective         TEXT NOT NULL DEFAULT '',
    instructions      TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS workflow_contact (
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'pending',
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
    direction         TEXT NOT NULL,
    subject           TEXT NOT NULL DEFAULT '',
    body_text         TEXT NOT NULL DEFAULT '',
    labels            JSONB NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'draft',
    is_classified     BOOLEAN NOT NULL DEFAULT FALSE,
    sent_at           TIMESTAMPTZ,
    received_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task (
    id            TEXT PRIMARY KEY,
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    email_id      TEXT REFERENCES email(id),
    description   TEXT NOT NULL,
    context       JSONB NOT NULL DEFAULT '{}',
    scheduled_at  TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    completed_at  TIMESTAMPTZ
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
);

CREATE INDEX IF NOT EXISTS idx_company_name ON company(LOWER(name));
CREATE INDEX IF NOT EXISTS idx_contact_domain ON contact(domain);
CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contact(company_id);
CREATE INDEX IF NOT EXISTS idx_workflow_account_id ON workflow(account_id);
CREATE INDEX IF NOT EXISTS idx_workflow_contact_contact_id ON workflow_contact(contact_id);
CREATE INDEX IF NOT EXISTS idx_task_workflow_id ON task(workflow_id);
CREATE INDEX IF NOT EXISTS idx_task_scheduled_at ON task(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_email_account_id ON email(account_id);
CREATE INDEX IF NOT EXISTS idx_email_contact_id ON email(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_workflow_id ON email(workflow_id);
CREATE INDEX IF NOT EXISTS idx_email_gmail_thread_id ON email(gmail_thread_id);
