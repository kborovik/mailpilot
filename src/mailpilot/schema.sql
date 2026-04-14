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
    rejected_reason       JSONB NOT NULL DEFAULT '[]',
    qualification_notes   TEXT,
    qualified_at          TIMESTAMPTZ,
    mail_provider         TEXT,
    mx_checked_at         TIMESTAMPTZ,
    firecrawl_enriched_at TIMESTAMPTZ,
    firecrawl_sources     JSONB NOT NULL DEFAULT '[]',
    hunter_searched_at    TIMESTAMPTZ,
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
    bouncer_status        TEXT,
    bouncer_score         INTEGER,
    bouncer_date          TEXT,
    firecrawl_sources     JSONB NOT NULL DEFAULT '[]',
    firecrawl_enriched_at TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS campaign (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    description       TEXT NOT NULL DEFAULT '',
    account_id        TEXT NOT NULL REFERENCES account(id),
    status            TEXT NOT NULL DEFAULT 'draft',
    template_subject  TEXT NOT NULL DEFAULT '',
    template_body     TEXT NOT NULL DEFAULT '',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email (
    id                TEXT PRIMARY KEY,
    gmail_message_id  TEXT UNIQUE,
    gmail_thread_id   TEXT,
    account_id        TEXT NOT NULL REFERENCES account(id),
    contact_id        TEXT REFERENCES contact(id),
    campaign_id       TEXT REFERENCES campaign(id),
    direction         TEXT NOT NULL,
    subject           TEXT NOT NULL DEFAULT '',
    body_text         TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'draft',
    is_classified     BOOLEAN NOT NULL DEFAULT FALSE,
    labels            JSONB NOT NULL DEFAULT '[]',
    sent_at           TIMESTAMPTZ,
    received_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_name ON company(LOWER(name));
CREATE INDEX IF NOT EXISTS idx_contact_domain ON contact(domain);
CREATE INDEX IF NOT EXISTS idx_contact_company_id ON contact(company_id);
CREATE INDEX IF NOT EXISTS idx_campaign_account_id ON campaign(account_id);
CREATE INDEX IF NOT EXISTS idx_email_account_id ON email(account_id);
CREATE INDEX IF NOT EXISTS idx_email_contact_id ON email(contact_id);
CREATE INDEX IF NOT EXISTS idx_email_campaign_id ON email(campaign_id);
CREATE INDEX IF NOT EXISTS idx_email_gmail_thread_id ON email(gmail_thread_id);
