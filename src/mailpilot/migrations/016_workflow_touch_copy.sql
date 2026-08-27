-- Add per-touch campaign copy JSONB on workflow (§V.194, §V.103, §V.134).
--
-- Empty list = all-LLM cadence (today). A row for N is harness-rendered copy
-- for that touch only. Import-only def field, hashed with the rest of the
-- wording set. Append after touch_interval_days so migrate-from-zero matches
-- schema.sql column-for-column (§V.108 identity).
ALTER TABLE workflow ADD COLUMN touch_copy JSONB NOT NULL DEFAULT '[]'::jsonb;
