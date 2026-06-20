-- Tags -> operator-maintained controlled vocabulary (§V.116).
--
-- Splits the legacy per-owner `tag` table into a vocabulary table (`tag`, one
-- row per distinct name, name globally unique §V.90) and a link table
-- (`tag_assignment`, one row per tag-and-owner pair). Each distinct legacy name
-- becomes one vocabulary row; each legacy tag row becomes one assignment.
--
-- The legacy rows are stashed in a session-temp table (dropped on commit), the
-- legacy `tag` is dropped to release its constraint/index names, and the new
-- `tag` is recreated with DDL identical to `schema.sql` so a migrate-from-zero
-- build is byte-identical to a fresh `schema.sql` build (§V.108 identity).
-- New ids are minted with PostgreSQL 18's uuidv7() (UUIDv7 per §C).

CREATE TEMP TABLE tag_legacy ON COMMIT DROP AS SELECT * FROM tag;

DROP TABLE tag;

CREATE TABLE tag (
    id              TEXT PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    disabled_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (disabled_reason IS NULL OR TRIM(disabled_reason) <> '')
);

CREATE TABLE tag_assignment (
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

CREATE UNIQUE INDEX idx_tag_assignment_contact_unique
    ON tag_assignment(tag_id, contact_id)
    WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX idx_tag_assignment_company_unique
    ON tag_assignment(tag_id, company_id)
    WHERE company_id IS NOT NULL;
CREATE INDEX idx_tag_assignment_tag ON tag_assignment(tag_id);
CREATE INDEX idx_tag_assignment_contact
    ON tag_assignment(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX idx_tag_assignment_company
    ON tag_assignment(company_id) WHERE company_id IS NOT NULL;

-- Vocabulary: one row per distinct name. An active legacy row wins (the name
-- stays enabled if any owner carried it active); otherwise the earliest
-- disabled row supplies the reason.
INSERT INTO tag (id, name, disabled_reason, created_at)
SELECT DISTINCT ON (name)
    uuidv7()::text, name, disabled_reason, created_at
FROM tag_legacy
ORDER BY name, (disabled_reason IS NOT NULL), created_at;

-- Assignments: one link per legacy row, joined to its vocabulary entry by name.
-- ON CONFLICT DO NOTHING collapses legacy active+disabled duplicates of the same
-- (owner, name) pair into a single assignment.
INSERT INTO tag_assignment (id, tag_id, contact_id, company_id, created_at)
SELECT uuidv7()::text, t.id, l.contact_id, l.company_id, l.created_at
FROM tag_legacy l
JOIN tag t ON t.name = l.name
ON CONFLICT DO NOTHING;
