-- App settings singleton (§V.181). Typed cols match Settings fields except
-- database_url (bootstrap only) and derived pubsub names. Null JSONB
-- google_application_credentials -> Application Default Credentials.

CREATE TABLE IF NOT EXISTS app_config (
    id                             TEXT PRIMARY KEY DEFAULT 'singleton'
                                   CHECK (id = 'singleton'),
    logfire_token                  TEXT NOT NULL DEFAULT '',
    environment                    TEXT NOT NULL DEFAULT 'dev'
                                   CHECK (environment IN ('dev', 'prd')),
    llm_provider                   TEXT NOT NULL DEFAULT 'xai'
                                   CHECK (llm_provider IN ('anthropic', 'xai')),
    anthropic_api_key              TEXT NOT NULL DEFAULT '',
    anthropic_model                TEXT NOT NULL DEFAULT 'claude-sonnet-5',
    anthropic_base_url             TEXT NOT NULL DEFAULT 'https://api.anthropic.com',
    anthropic_thinking             TEXT NOT NULL DEFAULT 'adaptive'
                                   CHECK (anthropic_thinking IN ('', 'adaptive')),
    anthropic_effort               TEXT NOT NULL DEFAULT 'high'
                                   CHECK (anthropic_effort IN
                                   ('', 'low', 'medium', 'high', 'xhigh', 'max')),
    anthropic_max_tokens           INTEGER NOT NULL DEFAULT 32768,
    xai_api_key                    TEXT NOT NULL DEFAULT '',
    xai_model                      TEXT NOT NULL DEFAULT 'grok-4.5',
    xai_api_host                   TEXT NOT NULL DEFAULT '',
    xai_reasoning_effort           TEXT NOT NULL DEFAULT 'medium'
                                   CHECK (xai_reasoning_effort IN
                                   ('low', 'medium', 'high')),
    xai_max_tokens                 INTEGER NOT NULL DEFAULT 32768,
    google_application_credentials JSONB,
    run_interval                   INTEGER NOT NULL DEFAULT 60,
    max_concurrent_tasks           INTEGER NOT NULL DEFAULT 10
);

INSERT INTO app_config (id) VALUES ('singleton')
ON CONFLICT (id) DO NOTHING;
