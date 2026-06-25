-- Rename `workflow.objective` to `workflow.goal` (§V.124, §V.103).
--
-- The field is the workflow success condition: the observable outcome that
-- concludes the enrollment once reached (§V.124). It keeps both readers --
-- the agent conclusion path and the inbound classification match key -- so the
-- rename is name-only, no type or default change.
--
-- Migration 001 still creates `objective` and this migration renames it, so a
-- migrate-from-zero build ends with column `goal` byte-identical to a fresh
-- `schema.sql` build (§V.108 identity). Migrations 001 through 005 stay
-- untouched.
--
-- PostgreSQL 18 catalogs the inline `NOT NULL` as a named constraint
-- (`workflow_objective_not_null`); `RENAME COLUMN` does not touch the
-- constraint name, so the constraint is renamed to the name a fresh
-- `schema.sql` build auto-generates for `goal` -- otherwise the identity
-- fingerprint diverges on the constraint name.

ALTER TABLE workflow RENAME COLUMN objective TO goal;
ALTER TABLE workflow
    RENAME CONSTRAINT workflow_objective_not_null TO workflow_goal_not_null;
