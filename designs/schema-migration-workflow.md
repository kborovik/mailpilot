# schema migration workflow

## Problem

Schema management is apply-once-on-empty + advisory-drift; unsafe near prod data.

Anchored behavior (`database.py:128-190`):
- `initialize_database()` runs per-connection (every CLI cmd + run loop). Probes `to_regclass('account')`:
  - empty DB -> exec all `schema.sql` (`CREATE TABLE IF NOT EXISTS`) + stamp `schema_metadata(version, hash)`
  - populated DB -> `hash(schema.sql)` vs recorded hash; mismatch -> `logfire.warn` + `operator_event("schema.drift")`. applies nothing, blocks nothing, proceeds.
- `make clean` = sole forward path for populated schema -> drop all + re-apply on empty (`database.py:155-159`).

Three holes, sharpened by "close to production":
1. no migration path -> any column/table change reaches populated DB only via `make clean` = total wipe. real crisis.
2. drift = file-hash proxy, advisory-only (§V.18/19) -> detects "schema.sql text differs from build-time", not live structure; never stops. code then runs `INSERT ... title` vs table w/o column -> psycopg error mid-batch in prod, not clean startup refusal.
3. provisioning = connection side-effect -> every invocation re-probes; fresh prod DB auto-built by whoever connects first.

## Proposal

A. new `db` noun (§I.cli) -> provisioning/migration explicit, off hot path:
- `mailpilot db init` -> provision empty DB: apply `schema.sql` + stamp `schema_metadata`. refuses if `account` exists (no `--force` data-loss footgun). idempotent no-op-with-message if already current.
- `mailpilot db migrate` -> apply pending `migrations/NNN_*.sql` in order, each in own transaction, record each in `schema_migrations`. no-op if none pending.
- `mailpilot db check` -> report `{recorded_hash, current_hash, applied: [...], pending: [...], verdict}`; exit 1 if `pending` | `drift`. scriptable deploy gate.

B. forward-only migration registry:
- `migrations/` @ package root (shipped in wheel). files `NNN_snake_description.sql`, monotonic int prefix, forward-only (no down-migrations).
- new table `schema_migrations(version INT PRIMARY KEY, name TEXT, applied_at TIMESTAMPTZ, mailpilot_version TEXT)`. non-singleton (cf. §V.89).
- `schema.sql` stays canonical declarative "current full schema" (fresh-DB build + hash identity).
- invariant: fresh `db init` from `schema.sql` == apply-all-migrations-from-zero. checkable -> prevents silent divergence.

C. three-state verdict (replaces binary drift):
- `current` -> hash matches & zero pending.
- `pending` -> migrations unapplied -> run `db migrate` (expected, not error).
- `drift` -> hash mismatch w/ no migration path (manual edit | DB ahead of code) -> dead-stop.

D. hot-path `initialize_database()` = connect + verify, not connect + provision:
- empty-DB auto-provision stays (data-loss-free; keeps `make clean` + test fixtures ergonomic).
- populated DB -> run gate (one cheap SELECT) -> tiered response per Decision Q1.

## Enforcement tiers & envelope codes

- tolerate drift (read-only diagnosis): `status`, `db check`.
- refuse on `drift`: `run` + every mutation -> `{"error":"schema_drift",...,"ok":false}` + exit 1.
- refuse on `pending`: `run` -> `{"error":"schema_migration_pending",...,"ok":false}` + exit 1.
- two distinct codes (remedy differs): `schema_drift` = investigate divergence; `schema_migration_pending` = run `db migrate`. both join §V.54 constraint->code family.

## Effect on in-flight SPEC items

- §V.18 narrowed -> drift no longer "warn, never silent" only; gates per tier (run + mutations refuse, status/`db check` tolerate).
- §V.19 unchanged -> hash stays identity/drift primitive, now augmented by migration ledger.
- §V.89 unchanged -> add `schema_migrations` (non-singleton) alongside `schema_metadata` id=1.
- §V.11 status `schema` block -> carry three-state `verdict` (not bare `drift` bool); applied/pending counts.
- new §V -> migration model + dead-stop gate + init==migrations identity. new §T -> impl. CLAUDE.md "auto-applied on first connection" line -> rewrite to db init/migrate.

## Design decisions

**Decision (Q1 drift scope):** tiered -- `status`/`db check` return on drift; `run` + mutations -> `schema_drift` envelope + exit 1. **Why:** operator must inspect drifted DB to choose remedy, but no write lands vs mismatched schema -> fail at startup not mid-batch in prod.

**Decision (Q2 model):** forward-only `migrations/NNN_*.sql` + `schema_migrations` ledger; no framework, no down-migrations. **Why:** YAGNI + "no ORM" (§C). ledger -> audit + idempotent re-run; forward-only fits single-prod-DB where rollback = restore-from-backup not down-script.

**Decision (Q3 provisioning):** keep auto-provision on genuinely-empty DB; add explicit `db init` + `db migrate` for populated advances; hot-path = connect + verify, provision only when `account` absent. **Why:** empty-DB apply is data-loss-free + keeps `make clean`/fixtures ergonomic; populated DB never mutates structure as connection side-effect.

**Decision (Q4 pending on run):** `mailpilot run` refuses to start on `pending` w/ distinct `schema_migration_pending` envelope + exit 1. **Why:** migrate-before-serve; serving writes vs half-migrated schema = the failure mode to prevent. distinct code -> remedy differs from drift.

## Success criterion

- column add ships as `migrations/NNN_*.sql`; `db migrate` advances populated DB w/ zero data loss; re-run = no-op.
- fresh `db init` (from `schema.sql`) & apply-all-migrations-from-empty -> byte-identical structure (test-enforced).
- drifted populated DB: `run`/mutations exit 1 `schema_drift`; `status`/`db check` still return.
- pending DB: `run` exits 1 `schema_migration_pending`; after `db migrate`, `run` starts.

## Out of scope

down-migrations; multi-DB/tenant; online/zero-downtime DDL choreography (trigger-deadlock hazard `database.py:155` stays per-migration authoring concern).
