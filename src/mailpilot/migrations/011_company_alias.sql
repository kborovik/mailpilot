-- Company domain aliases (§V.142): alternate domains resolve to a canonical
-- company. Domain space is shared with company.domain — never both owners.
-- Merge (§V.143) records the absorbed brand as an alias after tombstoning
-- the source company's domain so UNIQUE + shared-space invariants hold.

CREATE TABLE IF NOT EXISTS company_alias (
    domain      TEXT PRIMARY KEY,
    company_id  TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_company_alias_company_id
    ON company_alias(company_id);

-- Shared domain space: a string is either company.domain or company_alias.domain.
CREATE OR REPLACE FUNCTION enforce_company_domain_space() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'company_alias' THEN
        IF EXISTS (
            SELECT 1 FROM company
            WHERE LOWER(domain) = LOWER(NEW.domain)
        ) THEN
            RAISE EXCEPTION
                'domain % conflicts with company.domain', NEW.domain
                USING ERRCODE = 'unique_violation';
        END IF;
    ELSIF TG_TABLE_NAME = 'company' THEN
        IF EXISTS (
            SELECT 1 FROM company_alias
            WHERE LOWER(domain) = LOWER(NEW.domain)
        ) THEN
            RAISE EXCEPTION
                'domain % conflicts with company_alias.domain', NEW.domain
                USING ERRCODE = 'unique_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_company_alias_domain_space ON company_alias;
CREATE TRIGGER trg_company_alias_domain_space
    BEFORE INSERT OR UPDATE OF domain ON company_alias
    FOR EACH ROW EXECUTE FUNCTION enforce_company_domain_space();

DROP TRIGGER IF EXISTS trg_company_domain_space ON company;
CREATE TRIGGER trg_company_domain_space
    BEFORE INSERT OR UPDATE OF domain ON company
    FOR EACH ROW EXECUTE FUNCTION enforce_company_domain_space();
