-- Contact operator-only verification metadata (§V.144).
-- Durable audit (Bouncer status, source, etc.) never injected into agent prompts.
-- Default ContactView / load_contact_view omit this column; CLI
-- ``contact view --include-meta`` projects it for operators.

ALTER TABLE contact
    ADD COLUMN IF NOT EXISTS verification_meta JSONB;
