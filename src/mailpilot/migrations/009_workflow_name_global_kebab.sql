-- Make `workflow.name` a global natural key, kebab-shaped (§V.90, §V.107,
-- §V.103).
--
-- `workflow.name` is promoted to the canonical cross-environment identifier: a
-- workflow imports identically in dev and prod because both read the same
-- `*.toml` whose file stem equals `name` (§V.103). That demands one global
-- `name`, so the per-account `UNIQUE (account_id, name)` composite is dropped
-- for a global `UNIQUE (name)`. A kebab CHECK pins the shape the file stem
-- carries (lowercase, hyphen-separated, no dots/at-signs) so the polymorphic
-- resolver never confuses a name with a domain, email, or UUID (§V.107).
--
-- The constraint names match what a fresh `schema.sql` build auto-generates
-- (`workflow_name_key`, `workflow_name_check`) so a migrate-from-zero build
-- stays byte-identical in structure to a fresh `db init` (§V.108 identity). On
-- an empty workflow table the normalize UPDATE below is a no-op, so the
-- structural fingerprint is unchanged by the data step.

-- One-time normalize: fold every existing name to its kebab form. Lowercase,
-- collapse runs of non-alphanumerics to a single hyphen, trim edge hyphens.
-- "AI Engineering" -> "ai-engineering"; "MailPilot Demo" -> "mailpilot-demo".
UPDATE workflow
SET name = trim(both '-' from regexp_replace(lower(name), '[^a-z0-9]+', '-', 'g'));

-- Swap the per-account composite uniqueness for global uniqueness.
ALTER TABLE workflow DROP CONSTRAINT workflow_account_id_name_key;
ALTER TABLE workflow ADD CONSTRAINT workflow_name_key UNIQUE (name);

-- Pin the kebab shape (§V.103).
ALTER TABLE workflow
    ADD CONSTRAINT workflow_name_check
    CHECK (name ~ '^[a-z0-9]+(-[a-z0-9]+)*$');
