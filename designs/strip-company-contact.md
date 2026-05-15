# Design: strip `company` ∧ `contact` to bare minimum

## Intent

Reshape `company` ∧ `contact` entities → identity-only rows. Cold-email campaign minimum surface. Research / persona / firmographics offload to `note` (append-only annotation, already in schema). Claude Code uses web-research skills (firecrawl, web-search) ∧ pipes findings into `--note` flag at create time. ⊥ new CLI verbs; ⊥ new tables; ⊥ migration (`make clean` recreates per §T.13 / §T.34 precedent).

## Scope

In: `src/mailpilot/models.py`, `src/mailpilot/schema.sql`, `src/mailpilot/database.py`, `src/mailpilot/cli.py` (`company create`, `contact create`), tests, smoke-test fixtures.
Out: agent tool surface (`read_company` / `read_contact` use `model_dump()` ∴ inherit narrower shape automatically), routing, sync, workflow, enrollment, email, task.

## Column drops

### `company`

| col | drop | rationale |
|---|---|---|
| `domain_aliases` JSONB | y | multi-domain holdcos ≡ create separate rows |
| `linkedin` TEXT | y | URL → put in `--note` body |
| `industry` TEXT | y | segmentation → tag (`industry:water-treatment`) |
| `employee_count` INT | y | research blob → note |
| `founded_year` INT | y | research blob → note |
| `products_services` JSONB | y | research blob → note |
| `locations` JSONB | y | research blob → note |
| `company_type` TEXT | y | research blob → note |
| `recent_activity` TEXT | y | research blob → note |
| `qualification_notes` TEXT | y | redundant w/ `note` table |
| `profile_summary` TEXT | y | redundant w/ `note` table |

Survivors: `id`, `name`, `domain` (UNIQUE), `created_at`, `updated_at`. 11 cols removed.

### `contact`

| col | drop | rationale |
|---|---|---|
| `domain` TEXT | y | derived from email; `company_id` covers segmentation |
| `idx_contact_domain` INDEX | y | dead w/ `domain` col |
| `email_type` TEXT | y | role-vs-personal → tag (`address:role`) |
| `position` TEXT | y | research blob → note |
| `seniority` TEXT | y | research blob → note |
| `department` TEXT | y | research blob → note |
| `profile_summary` TEXT | y | redundant w/ `note` table |
| `linkedin` TEXT | y | URL → put in `--note` body |

Survivors: `id`, `email` (UNIQUE), `company_id` FK, `first_name`, `last_name`, `created_at`, `updated_at`, ∧ `disabled_reason` (see next section). 8 cols removed.

## Status collapse

`status` enum CHECK (`active|bounced|unsubscribed`) + `status_reason` TEXT → single `disabled_reason TEXT` (nullable, default NULL).

- `disabled_reason IS NULL` ≡ contact active.
- `disabled_reason IS NOT NULL` ≡ contact disabled; string carries semantics (e.g. `"bounced: 5.1.1 recipient address rejected"`, `"unsubscribed: 2026-05-14 reply"`).
- ⊥ CHECK constraint, ⊥ enum.
- Index: `CREATE INDEX idx_contact_active ON contact (id) WHERE disabled_reason IS NULL` (partial; cold-email enrollment scan path).

Agent tool `disable_contact(email, reason)` writes `disabled_reason = reason`. `mailpilot contact list` default = `WHERE disabled_reason IS NULL`; opt-in to see all via `--include-disabled` flag (impl detail, ⊥ new design knob).

## CLI surface

`mailpilot company create --name STR --domain STR [--note STR]`
`mailpilot contact create --email STR [--first-name STR] [--last-name STR] [--company-id ID] [--note STR]`

`--note STR` semantics:
- non-empty → after row insert, append 1 `note` row (`company_id` ∨ `contact_id` set, `body=<note>`), same `psycopg.Connection` txn ∴ atomic.
- empty ∨ omitted → ⊥ note row.
- single `cli_mutation("<noun>", "create")` span wraps both inserts (§V.47); `operator_event` `changed` list extended w/ `"note"` when note inserted.

Why: cold-email research happens upstream in Claude Code; create + annotate ≡ 1 op ∴ 1 CLI call. Two-step (`create` → `note add`) viable but doubles invocation cost when Claude Code already holds the research blob.

`mailpilot company update` / `mailpilot contact update`: ⊥ `--note` flag (use `note add`); column drops narrow update fields automatically.

## Model + summary shape post-strip

```
Company:        id, name, domain, created_at, updated_at
CompanySummary: id, name, domain, created_at   # drop industry, employee_count

Contact:        id, email, company_id, first_name, last_name,
                disabled_reason, created_at, updated_at
ContactSummary: id, email, first_name, last_name, company_id,
                disabled_reason, created_at   # rename `status` → `disabled_reason`
```

`ActivitySummary` FK columns (§V.51): unaffected.

## Export / import contract (§V.39)

`company export` / `company import` payload row keys = `{name, domain}` ∋ `[note?]` field carrying optional single-string note body (idempotent re-import: present-but-equal note ⊥ duplicate; absent ⊥ append). Actually no — `note` rows ≡ append-only per §V.9; round-trip on unchanged DB must yield 0 mutations. ∴ export ⊥ emit `note` field; import ⊥ accept `note` field. Note creation is interactive-only (CLI `--note` flag). Round-trip stays clean.

`contact export` / `contact import`: same. Drop `domain`, `status`, `status_reason` from export shape; add `disabled_reason` (nullable). Idempotent.

## Migration

⊥ migration. `make clean` recreates schema. Touch points enumerated in §T row.

## Touch surface

- `src/mailpilot/models.py` — `Company` -11, `CompanySummary` -2, `Contact` -8 (status/status_reason → disabled_reason), `ContactSummary` rename `status` → `disabled_reason`, drop `domain`.
- `src/mailpilot/schema.sql` — column drops, `idx_contact_domain` drop, CHECK drop on `contact.status`, add `idx_contact_active` partial.
- `src/mailpilot/database.py` — narrow `create_company` (unchanged signature; row already minimal), narrow `create_contact` (drop `domain` param), narrow `create_or_get_contact_by_email` (drop `email.split("@", 1)[1]` line), narrow `list_companies` / `search_companies` SELECTs, narrow `list_contacts` SELECT (rename status → disabled_reason), add `disable_contact(connection, contact_id, reason) → Contact`.
- `src/mailpilot/cli.py` — `company create` add `--note` opt + atomic note insert + `cli_mutation` span (§V.47); `contact create` same; `contact list` default-filter `disabled_reason IS NULL` + `--include-disabled` flag.
- `src/mailpilot/agent/tools.py` — `disable_contact` tool body: swap `status` write → `disabled_reason` write; sig unchanged.
- Tests — `test_database.py`, `test_cli.py`, `test_cli_telemetry.py`, `test_agent_tools.py`, fixtures.
- Smoke-test fixtures (`tests/fixtures/contacts-*.json` if any) — narrow shape.

## §V impact

⊥ new §V. Single §T row enumerates column drops + collapse + `--note` flag impl.

Existing invariants unaffected:
- §V.5 envelope shape: `company create` / `contact create` still emit singular envelope; `--note` ⊥ change envelope.
- §V.6 Summary projections: narrower, still complies.
- §V.8 tag/note XOR: unchanged.
- §V.9 append-only note/activity: unchanged.
- §V.39 export/import idempotency: payload shape narrows; round-trip stays clean (note ∉ payload).
- §V.47 CRM CLI telemetry: extended to cover `create + note` combined mutation (single span, single `operator_event` w/ `changed=["name","domain","note"]` ∨ `["email","first_name","note"]`).
- §V.51 ActivitySummary FK columns: unaffected.

## Open Questions

(none — converged 2026-05-15)

## Apply path

`/sdd:spec designs/strip-company-contact.md` → fold-in shortcut → appends §T row, deletes this draft.
