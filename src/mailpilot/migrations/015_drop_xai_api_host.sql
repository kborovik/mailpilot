-- Drop leftover xai_api_host (§V.191). Host is code-pinned to the SDK
-- official endpoint; leftover empty values were DNS-bound as the API key.

ALTER TABLE app_config DROP COLUMN IF EXISTS xai_api_host;
