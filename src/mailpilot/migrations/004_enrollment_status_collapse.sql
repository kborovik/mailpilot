-- Collapse enrollment.status to {active, disabled} (§V.15, drop `paused`).
--
-- `paused` and `disabled` were redundant operator-halt states; only `active`
-- enrollments run (§V.83), so every operator halt is now `disabled`. Existing
-- paused rows fold to disabled, carrying a literal reason so the disabled-row
-- coupling CHECK holds (disabled is true exactly when disabled_reason is set).
--
-- The status CHECK then tightens to two values. The activity-type CHECK gains
-- `enrollment_enabled` (the re-enable mirror of `enrollment_disabled`);
-- `enrollment_paused` / `enrollment_resumed` stay in the enum for historical
-- timeline rows. Constraints are dropped and re-added under their original
-- auto-generated names so a migrate-from-zero build stays byte-identical to a
-- fresh schema.sql build (§V.108 identity).

UPDATE enrollment
SET status = 'disabled',
    disabled_reason = 'paused: collapsed to disabled',
    updated_at = CURRENT_TIMESTAMP
WHERE status = 'paused';

ALTER TABLE enrollment
    DROP CONSTRAINT enrollment_status_check,
    ADD CONSTRAINT enrollment_status_check
        CHECK (status IN ('active', 'disabled'));

ALTER TABLE activity
    DROP CONSTRAINT activity_type_check,
    ADD CONSTRAINT activity_type_check
        CHECK (type IN (
            'email_sent', 'email_received',
            'note_added', 'tag_added', 'tag_removed',
            'tag_disabled',
            'status_changed',
            'enrollment_added',
            'enrollment_completed', 'enrollment_failed',
            'enrollment_paused', 'enrollment_resumed',
            'enrollment_disabled', 'enrollment_enabled'
        ));
