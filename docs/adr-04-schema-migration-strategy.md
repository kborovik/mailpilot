# ADR-04: Schema Migration Strategy

## Status

Accepted

## Context

The schema is currently applied via `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` in `schema.sql`, executed on every connection by `initialize_database()`. This is idempotent for initial creation but cannot handle `ALTER TABLE`, column additions, type changes, or constraint modifications.

As features are implemented, schema changes will be needed. A migration strategy must be chosen before the first `ALTER TABLE`.

Requirements:

- Raw SQL migrations (no ORM, no query builder -- matches the `psycopg` + raw SQL pattern)
- Minimal dependencies (YAGNI principle)
- Forward-only (no down migrations -- rollback via git revert + manual fix)
- Works with both development and test databases
- Simple enough that an LLM agent can generate and apply migrations

## Decision

**Numbered SQL files** applied in order, tracked by a `schema_version` table. No external dependency.

### Migration Files

```
migrations/
  001_initial.sql
  002_add_cooldown_minutes.sql
  003_add_email_headers.sql
```

Each file contains raw SQL. File names are `NNN_description.sql` where `NNN` is a zero-padded sequence number. The description is for humans only -- the system uses the number.

### Version Tracking

A `schema_version` table tracks which migrations have been applied:

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

### Migration Runner

`initialize_database()` in `database.py` replaces direct `schema.sql` execution with:

1. Create `schema_version` table if not exists
2. Read the current max version from `schema_version` (0 if empty)
3. Scan `migrations/` directory for `.sql` files with version > current
4. Apply each in order within a transaction
5. Insert a row into `schema_version` for each applied migration
6. If any migration fails, the transaction rolls back -- no partial state

### Initial Migration

`migrations/001_initial.sql` contains the current `schema.sql` content verbatim. Existing databases that already have the tables get version 1 inserted manually (or via a bootstrap check: if tables exist but `schema_version` is empty, insert version 1 and skip `001_initial.sql`).

### Creating New Migrations

1. Create `migrations/NNN_description.sql` with the next sequence number
2. Write the `ALTER TABLE` / `CREATE INDEX` / etc. SQL
3. Run `mailpilot status` (or any command) -- migration applies automatically
4. Commit the migration file

## Consequences

### Positive

- Zero dependencies -- just SQL files and a version table
- Matches the raw SQL pattern used throughout `database.py`
- Forward-only simplifies reasoning (no paired up/down files)
- Automatic on connection -- no separate `migrate` command needed
- LLM agents can generate migration files trivially

### Negative

- No down migrations -- rollback requires manual SQL or a new forward migration
- No checksums -- if a migration file is edited after application, no warning (acceptable for a single-developer project)
- Sequence numbers can conflict in parallel branches (resolve at merge time)
