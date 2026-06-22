-- Drop the dead `tag_disabled` activity type (§V.10, §V.17).
--
-- Vocabulary-tag disable/enable write no activity: an activity targets a
-- contact or company (§V.17) and a `tag` row owns neither, so `tag_disabled`
-- was never emitted and no historical row carries it. The activity-type CHECK
-- therefore tightens with no fold, unlike the enrollment collapse (004) that
-- retained `paused`/`resumed` for historical timeline rows.
--
-- The constraint is dropped and re-added under its original auto-generated
-- name so a migrate-from-zero build stays byte-identical to a fresh schema.sql
-- build (§V.108 identity).

ALTER TABLE activity
    DROP CONSTRAINT activity_type_check,
    ADD CONSTRAINT activity_type_check
        CHECK (type IN (
            'email_sent', 'email_received',
            'note_added', 'tag_added', 'tag_removed',
            'status_changed',
            'enrollment_added',
            'enrollment_completed', 'enrollment_failed',
            'enrollment_paused', 'enrollment_resumed',
            'enrollment_disabled', 'enrollment_enabled'
        ));
