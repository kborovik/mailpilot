-- Add the system-owned touch-cadence def fields to `workflow` (§V.136, §V.103).
--
-- `touches` = total sends in the sequence; `touch_interval_days` = the spacing
-- between them. The pair is nullable: both NULL means single-touch, no
-- automatic follow-up. The both-or-neither CHECK forbids a half-configured
-- cadence, so the engine never reads a `touches` with no interval. The columns
-- append after `updated_at` so a migrate-from-zero build matches a fresh
-- `schema.sql` build column-for-column (§V.108 identity); the CHECK carries an
-- explicit name so its `conname` agrees on both sides (mirrors migration 009).
ALTER TABLE workflow ADD COLUMN touches INTEGER;
ALTER TABLE workflow ADD COLUMN touch_interval_days INTEGER;
ALTER TABLE workflow ADD CONSTRAINT workflow_touch_cadence_check CHECK (
    ((touches IS NULL) = (touch_interval_days IS NULL))
    AND (touches IS NULL OR touches > 0)
    AND (touch_interval_days IS NULL OR touch_interval_days > 0)
);

-- One-time cutover (§V.136): fold the legacy prose touch number into structured
-- context so runtime dispatch keys on `context.touch`, never a parsed
-- description. The parse lives and dies here -- production carries exactly 44
-- pending `Touch N of M` follow-up tasks on 2026-07-02; first-touch tasks
-- already carry `trigger = enrollment_schedule` and dispatch as touch 1 with no
-- rewrite. Only pending rows fold; completed / cancelled history stays as sent.
UPDATE task
SET context = context || jsonb_build_object(
    'touch', substring(description FROM '^Touch ([0-9]+)')::int
)
WHERE status = 'pending'
  AND description ~ '^Touch [0-9]+ of [0-9]+';
