-- Add company.disabled_reason for company soft-disable.
-- Non-NULL marks a disabled company (carries the human reason); cleared to
-- re-enable. Appended last so the live structure matches a fresh schema.sql
-- build column-for-column (forward-only migration identity).
ALTER TABLE company ADD COLUMN IF NOT EXISTS disabled_reason TEXT;
