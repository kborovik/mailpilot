-- Per-account email signature fields (§V.151).
-- Nested CLI projection signature:{full_name,title,website,phone} (null when
-- all empty). display_name stays From-header only (not aliased to full_name).
-- Signature is harness-appended to outbound MIME; never persisted into email.body.

ALTER TABLE account
    ADD COLUMN IF NOT EXISTS signature_full_name TEXT,
    ADD COLUMN IF NOT EXISTS signature_title TEXT,
    ADD COLUMN IF NOT EXISTS signature_website TEXT,
    ADD COLUMN IF NOT EXISTS signature_phone TEXT;
