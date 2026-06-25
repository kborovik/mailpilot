-- Add the `meeting` + `meeting_attendee` tables (§V.125, §V.108).
--
-- A meeting is a first-class entity peer to email: one row per Google Calendar
-- event, keyed on `google_event_id` (nullable-unique, idempotent ingest,
-- mirrors email.gmail_message_id §V.90). `meeting_attendee` links one meeting
-- to >=1 contact (UNIQUE per pair, mirrors tag_assignment §V.116). The `status`
-- column is operator record-keeping only and gates nothing (§V.125).
--
-- DDL is byte-identical to the `meeting` + `meeting_attendee` block in
-- schema.sql, so a migrate-from-zero build ends structurally identical to a
-- fresh `schema.sql` build (§V.108 identity). Migrations 001 through 006 stay
-- untouched.

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
