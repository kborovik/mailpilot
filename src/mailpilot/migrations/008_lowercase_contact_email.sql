-- Canonicalize `contact.email` to lowercase + merge case-variant duplicates
-- (§V.90, §B.121).
--
-- `contact.email` is the natural key (§V.90). The write path now lowercases
-- `email` before every match|insert, so a case-variant `From` (Outlook/Exchange
-- recase the local-part) resolves to the canonical row instead of minting a bare
-- duplicate (§B.121). This migration repairs any duplicate a pre-fix build
-- already minted: it folds every case-variant contact onto one canonical
-- lowercase row, repoints the child rows, drops the duplicates, then lowercases
-- every surviving address.
--
-- `contact.email TEXT UNIQUE` stays case-sensitive -- the dedup guard is the
-- write-path lowercase, NOT the constraint (§V.90). This migration changes data
-- only, no structure, so a fresh `db init` from `schema.sql` stays byte-identical
-- in structure to migrate-from-zero (§V.108 identity). On an empty contact table
-- every step below is a no-op.

-- Canonical row per lowercased email = earliest-created (tie-break: smallest id).
CREATE TEMP TABLE contact_email_merge ON COMMIT DROP AS
WITH ranked AS (
    SELECT
        id,
        lower(email) AS lower_email,
        row_number() OVER (
            PARTITION BY lower(email)
            ORDER BY created_at, id
        ) AS rank
    FROM contact
),
canonical AS (
    SELECT lower_email, id AS canonical_id FROM ranked WHERE rank = 1
)
SELECT ranked.id AS duplicate_id, canonical.canonical_id
FROM ranked
JOIN canonical ON canonical.lower_email = ranked.lower_email
WHERE ranked.id <> canonical.canonical_id;

-- Enrollment carries UNIQUE (workflow_id, contact_id). When a duplicate and the
-- canonical share a workflow, repointing the duplicate enrollment onto the
-- canonical contact would violate that constraint. Repoint the duplicate
-- enrollment's tasks onto the canonical enrollment, then drop the now-childless
-- duplicate enrollment so the contact repoint below stays conflict-free.
CREATE TEMP TABLE enrollment_merge ON COMMIT DROP AS
SELECT dup.id AS duplicate_enrollment_id, can.id AS canonical_enrollment_id
FROM contact_email_merge m
JOIN enrollment dup ON dup.contact_id = m.duplicate_id
JOIN enrollment can
    ON can.contact_id = m.canonical_id
   AND can.workflow_id = dup.workflow_id;

UPDATE task t
SET enrollment_id = em.canonical_enrollment_id
FROM enrollment_merge em
WHERE t.enrollment_id = em.duplicate_enrollment_id;

DELETE FROM enrollment e
USING enrollment_merge em
WHERE e.id = em.duplicate_enrollment_id;

-- Repoint the remaining (non-colliding) enrollments onto the canonical contact.
UPDATE enrollment e
SET contact_id = m.canonical_id
FROM contact_email_merge m
WHERE e.contact_id = m.duplicate_id;

-- Repoint children with no UNIQUE on contact_id.
UPDATE task t SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE t.contact_id = m.duplicate_id;

UPDATE email x SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE x.contact_id = m.duplicate_id;

UPDATE activity a SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE a.contact_id = m.duplicate_id;

UPDATE note n SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE n.contact_id = m.duplicate_id;

-- tag_assignment carries UNIQUE (tag_id, contact_id): drop a duplicate's link
-- the canonical already holds, then repoint the rest.
DELETE FROM tag_assignment ta
USING contact_email_merge m
WHERE ta.contact_id = m.duplicate_id
  AND EXISTS (
      SELECT 1 FROM tag_assignment keep
      WHERE keep.contact_id = m.canonical_id
        AND keep.tag_id = ta.tag_id
  );

UPDATE tag_assignment ta SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE ta.contact_id = m.duplicate_id;

-- meeting_attendee carries UNIQUE (meeting_id, contact_id): same treatment.
DELETE FROM meeting_attendee ma
USING contact_email_merge m
WHERE ma.contact_id = m.duplicate_id
  AND EXISTS (
      SELECT 1 FROM meeting_attendee keep
      WHERE keep.contact_id = m.canonical_id
        AND keep.meeting_id = ma.meeting_id
  );

UPDATE meeting_attendee ma SET contact_id = m.canonical_id
FROM contact_email_merge m WHERE ma.contact_id = m.duplicate_id;

-- Drop the folded duplicate contacts, then lowercase every survivor.
DELETE FROM contact c
USING contact_email_merge m
WHERE c.id = m.duplicate_id;

UPDATE contact SET email = lower(email) WHERE email <> lower(email);
