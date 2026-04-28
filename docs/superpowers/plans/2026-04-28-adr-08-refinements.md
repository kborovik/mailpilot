# ADR-08 Refinements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the design refinements proposed in GitHub issue #102 plus the enrollment-status collapse from comment [#4334976677](https://github.com/kborovik/mailpilot/issues/102#issuecomment-4334976677). Replace polymorphic tag/note FKs with nullable typed FKs, allow company-only activity rows, add structured FK columns and composite timeline indexes on activity, rename `workflow_*` activity types to `enrollment_*`, collapse `enrollment.status` to `(active, paused)`, replace the agent's `update_enrollment_status` tool with `record_enrollment_outcome` (activity-only), make tag/note + activity writes atomic, tighten tag normalization, and rewrite ADR-08 to match implemented behavior.

**Architecture:** Bottom-up. Two slices keep the build green at boundaries:

- **Slice A** rewrites `schema.sql` and `models.py` together, since the type changes propagate.
- **Slice B** rewrites the affected `database.py` functions (Tag, Note, Activity, Enrollment) and adds atomic helpers.
- **Slice C** updates every caller (`routing.py`, `email_ops.py`, `sync.py`, `cli.py`, `agent/tools.py`, `agent/invoke.py`).
- **Slice D** rewrites the ADR, updates `CLAUDE.md`, and runs end-to-end verification.

Schema changes are applied via direct edits to `src/mailpilot/schema.sql` plus `make clean` (the project does not yet have a migration runner -- see ADR-05). This matches the precedent set by `docs/superpowers/plans/2026-04-25-enrollment-rename.md`.

**Tech Stack:** Python 3.14, PostgreSQL 18, psycopg, Pydantic, Click, basedpyright strict, ruff, pytest.

**Spec:** GitHub issue #102 (`gh issue view 102`) + comment [#4334976677](https://github.com/kborovik/mailpilot/issues/102#issuecomment-4334976677). Renamed ADR file: `docs/adr-08-crm-design.md`.

---

## Conventions for this plan

**Refactor TDD pattern.** Existing tests already cover Tag, Note, Activity, and Enrollment behavior. For renames and shape changes, the cycle is: update the test (it fails because the impl still uses the old shape), update the impl (test passes), commit. For genuinely new behavior (atomic helpers, tag normalization regex, company-only activity rows, `record_enrollment_outcome`, `enrollment_paused`/`resumed` activity types), write the failing test first.

**Schema-coupled tests.** Some tasks change schema, models, and DB functions in a single coordinated edit. Within Slice A and B, intermediate commits may temporarily break unrelated tests -- that's expected. Each slice ends with `make check` clean.

**Migration via `make clean`.** After `schema.sql` changes, run:

```bash
make clean
DATABASE_URL=postgresql://localhost/mailpilot_test \
    uv run python -c "from mailpilot.database import initialize_database; initialize_database('postgresql://localhost/mailpilot_test').close()"
```

This drops and recreates both the dev DB (`mailpilot`) and the test DB (`mailpilot_test`). Only required after Task A1.

**Commit cadence.** Commit at the end of every task. Use Conventional Commits: `refactor(area): ...` for renames, `feat(area): ...` for new behavior, `fix(area): ...` for genuine bug fixes that surface during the refactor, `docs(adr): ...` for ADR rewrites.

---

## File Structure

| File                            | Disposition                                                                                                                                           |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/mailpilot/schema.sql`      | Modify -- tag/note FKs, activity columns + indexes, enrollment status                                                                                 |
| `src/mailpilot/models.py`       | Modify -- Tag/Note/Activity/Enrollment model shapes, type unions                                                                                      |
| `src/mailpilot/database.py`     | Modify -- Tag/Note/Activity/Enrollment CRUD; new atomic helpers; new `_normalize_tag_name`                                                            |
| `src/mailpilot/routing.py`      | Modify -- `workflow_assigned` -> `enrollment_added`, pass `workflow_id` FK                                                                            |
| `src/mailpilot/email_ops.py`    | Modify -- drop `_activate_enrollment_if_pending`; pass `email_id`/`workflow_id` FKs in `email_sent` activity                                          |
| `src/mailpilot/sync.py`         | Modify -- pass `email_id` FK in `email_received` activity                                                                                             |
| `src/mailpilot/agent/tools.py`  | Modify -- replace `update_enrollment_status` with `record_enrollment_outcome` (activity only)                                                         |
| `src/mailpilot/agent/invoke.py` | Modify -- wrapper rename, `_TOOLS`, `_SYSTEM_PREFIX`                                                                                                  |
| `src/mailpilot/cli.py`          | Modify -- `_ACTIVITY_TYPES`, tag/note commands use atomic helpers, activity create supports company-only, enrollment update only allows active/paused |
| `tests/test_database.py`        | Modify -- Tag/Note/Activity/Enrollment tests                                                                                                          |
| `tests/test_cli.py`             | Modify -- tag/note/activity/enrollment CLI tests                                                                                                      |
| `tests/test_routing.py`         | Modify -- `enrollment_added` activity assertions                                                                                                      |
| `tests/test_email_ops.py`       | Modify -- drop pending-state activation expectations; assert `email_id`/`workflow_id` in email_sent activity                                          |
| `tests/test_sync.py`            | Modify -- assert `email_id` in email_received activity                                                                                                |
| `tests/test_agent_tools.py`     | Modify -- `record_enrollment_outcome` tests                                                                                                           |
| `tests/test_agent_invoke.py`    | Modify -- tool registration, prompt prefix                                                                                                            |
| `tests/test_models.py`          | Modify -- model shape tests                                                                                                                           |
| `docs/adr-08-crm-design.md`     | Rewrite -- match implemented behavior, document new FK columns and tag normalization                                                                  |
| `CLAUDE.md`                     | Modify -- CRM Model section, CLI reference block                                                                                                      |

---

## Slice A: Schema + Models

### Task A1: Rewrite `schema.sql` for tag/note/activity/enrollment

**Files:**

- Modify: `src/mailpilot/schema.sql:150-193` (activity, tag, note tables) and `src/mailpilot/schema.sql:73-84` (enrollment table)

- [ ] **Step 1: Rewrite the activity table block**

Replace lines 150-169 with:

```sql
CREATE TABLE IF NOT EXISTS activity (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    email_id        TEXT REFERENCES email(id),
    workflow_id     TEXT REFERENCES workflow(id),
    task_id         TEXT REFERENCES task(id),
    type            TEXT NOT NULL
                    CHECK (type IN (
                        'email_sent', 'email_received',
                        'note_added', 'tag_added', 'tag_removed',
                        'status_changed',
                        'enrollment_added',
                        'enrollment_completed', 'enrollment_failed',
                        'enrollment_paused', 'enrollment_resumed'
                    )),
    summary         TEXT NOT NULL DEFAULT '',
    detail          JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (contact_id IS NOT NULL OR company_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_activity_contact_timeline
    ON activity(contact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_company_timeline
    ON activity(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type ON activity(type);
```

Notes:

- `contact_id` is now nullable; CHECK enforces at least one of contact/company.
- `email_id`, `workflow_id`, `task_id` are real nullable FKs (suggestion #5).
- Composite indexes replace the single-column `idx_activity_contact_id`, `idx_activity_company_id`, and `idx_activity_created_at` (suggestion #6). Type index is kept for `--type` filters.
- Activity type list is the new vocabulary -- `workflow_*` removed, `enrollment_*` added.

- [ ] **Step 2: Rewrite the tag table block**

Replace lines 171-182 with:

```sql
CREATE TABLE IF NOT EXISTS tag (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    name            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (contact_id IS NOT NULL AND company_id IS NULL)
        OR
        (contact_id IS NULL AND company_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_contact_unique
    ON tag(contact_id, name) WHERE contact_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_company_unique
    ON tag(company_id, name) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tag_name ON tag(name);
```

The XOR CHECK enforces exactly one of contact/company. Two partial unique indexes replace the polymorphic UNIQUE constraint.

- [ ] **Step 3: Rewrite the note table block**

Replace lines 184-193 with:

```sql
CREATE TABLE IF NOT EXISTS note (
    id              TEXT PRIMARY KEY,
    contact_id      TEXT REFERENCES contact(id),
    company_id      TEXT REFERENCES company(id),
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (contact_id IS NOT NULL AND company_id IS NULL)
        OR
        (contact_id IS NULL AND company_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_note_contact_id ON note(contact_id) WHERE contact_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_note_company_id ON note(company_id) WHERE company_id IS NOT NULL;
```

- [ ] **Step 4: Tighten the enrollment status check**

Replace the enrollment table block (lines 73-84) with:

```sql
CREATE TABLE IF NOT EXISTS enrollment (
    workflow_id   TEXT NOT NULL REFERENCES workflow(id),
    contact_id    TEXT NOT NULL REFERENCES contact(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'paused')),
    reason        TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (workflow_id, contact_id)
);
```

Default flips from `'pending'` to `'active'`. Outcomes (`completed`, `failed`) move to the activity timeline.

- [ ] **Step 5: Drop and reapply schema for both DBs**

Run:

```bash
make clean
DATABASE_URL=postgresql://localhost/mailpilot_test \
    uv run python -c "from mailpilot.database import initialize_database; initialize_database('postgresql://localhost/mailpilot_test').close()"
```

Expected: no error from either run. The dev DB (`mailpilot`) is dropped and recreated with the new schema; the test DB is initialized for the upcoming pytest runs.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/schema.sql
git commit -m "refactor(schema): nullable FKs on tag/note/activity, enrollment status collapse"
```

---

### Task A2: Update `models.py` shapes and type unions

**Files:**

- Modify: `src/mailpilot/models.py:129-140` (Enrollment), `:228-294` (Activity, Tag, Note, EntityType)

- [ ] **Step 1: Update the test for `EnrollmentStatus`**

Edit `tests/test_models.py`. Find the existing test asserting that `EnrollmentStatus` accepts `"pending"` / `"completed"` / `"failed"` and rewrite it to assert only `"active"` and `"paused"` are accepted:

```python
def test_enrollment_status_literal_is_active_or_paused() -> None:
    """EnrollmentStatus collapsed to operational state only (#102)."""
    from typing import get_args

    from mailpilot.models import EnrollmentStatus

    assert set(get_args(EnrollmentStatus)) == {"active", "paused"}
```

If a similar test exists with the old set, replace it. If none exists, add this one.

- [ ] **Step 2: Update the test for `ActivityType`**

In the same file, replace any `ActivityType` test with:

```python
def test_activity_type_literal_uses_enrollment_vocabulary() -> None:
    """workflow_* renamed to enrollment_*; pause/resume added (#102)."""
    from typing import get_args

    from mailpilot.models import ActivityType

    assert set(get_args(ActivityType)) == {
        "email_sent",
        "email_received",
        "note_added",
        "tag_added",
        "tag_removed",
        "status_changed",
        "enrollment_added",
        "enrollment_completed",
        "enrollment_failed",
        "enrollment_paused",
        "enrollment_resumed",
    }
```

- [ ] **Step 3: Update the test for `Tag` shape**

In the same file:

```python
def test_tag_uses_nullable_contact_company_fks() -> None:
    """Polymorphic entity_type/entity_id replaced with typed FKs (#102 suggestion 1)."""
    from datetime import UTC, datetime

    from mailpilot.models import Tag

    contact_tag = Tag(
        id="t1",
        contact_id="c1",
        company_id=None,
        name="prospect",
        created_at=datetime.now(UTC),
    )
    assert contact_tag.contact_id == "c1"
    assert contact_tag.company_id is None
    assert not hasattr(contact_tag, "entity_type")
    assert not hasattr(contact_tag, "entity_id")
```

- [ ] **Step 4: Update the test for `Note` shape**

```python
def test_note_uses_nullable_contact_company_fks() -> None:
    """Polymorphic entity_type/entity_id replaced with typed FKs (#102 suggestion 1)."""
    from datetime import UTC, datetime

    from mailpilot.models import Note

    note = Note(
        id="n1",
        company_id="co1",
        contact_id=None,
        body="Met at conf",
        created_at=datetime.now(UTC),
    )
    assert note.company_id == "co1"
    assert note.contact_id is None
```

- [ ] **Step 5: Update the test for `Activity` shape**

```python
def test_activity_supports_company_only_and_structured_fks() -> None:
    """contact_id is nullable; email_id/workflow_id/task_id added (#102 suggestions 2, 5)."""
    from datetime import UTC, datetime

    from mailpilot.models import Activity

    company_activity = Activity(
        id="a1",
        contact_id=None,
        company_id="co1",
        type="note_added",
        summary="Company note",
        detail={},
        created_at=datetime.now(UTC),
    )
    assert company_activity.contact_id is None
    assert company_activity.company_id == "co1"

    email_activity = Activity(
        id="a2",
        contact_id="c1",
        company_id=None,
        email_id="e1",
        workflow_id="wf1",
        type="email_sent",
        summary="Subject",
        detail={},
        created_at=datetime.now(UTC),
    )
    assert email_activity.email_id == "e1"
    assert email_activity.workflow_id == "wf1"
    assert email_activity.task_id is None
```

- [ ] **Step 6: Run the model tests, expect failures**

Run: `uv run pytest tests/test_models.py -v`
Expected: the four new tests fail (old shapes still in place).

- [ ] **Step 7: Update `Enrollment` and `EnrollmentStatus` in `models.py`**

Replace lines 129-140:

```python
EnrollmentStatus = Literal["active", "paused"]


class Enrollment(BaseModel):
    """A contact's binding to a workflow.

    Status is operational state only -- ``active`` (agent considers this
    contact when the workflow runs) or ``paused`` (operator/agent has
    suspended). Outcomes (completed/failed) live in the activity timeline,
    not in this row.
    """

    workflow_id: str
    contact_id: str
    status: EnrollmentStatus = "active"
    reason: str = ""
    created_at: datetime
    updated_at: datetime
```

`EnrollmentSummary` already uses `EnrollmentStatus` -- no change to its shape but it now reflects the collapsed set automatically.

- [ ] **Step 8: Update `ActivityType` in `models.py`**

Replace lines 228-238:

```python
ActivityType = Literal[
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
]
```

- [ ] **Step 9: Update `Activity` and `ActivitySummary` in `models.py`**

Replace lines 241-261:

```python
class Activity(BaseModel):
    """Chronological event in a contact or company timeline.

    Either ``contact_id`` or ``company_id`` must be set (or both, for
    contact events that should also surface in the company timeline).
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``)
    let reports join activity to source records without parsing
    ``detail`` JSON.
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    email_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    type: ActivityType
    summary: str = ""
    detail: dict[str, object] = {}
    created_at: datetime


class ActivitySummary(BaseModel):
    """List-view projection of `Activity`."""

    id: str
    contact_id: str | None
    company_id: str | None
    type: ActivityType
    summary: str
    created_at: datetime
```

- [ ] **Step 10: Replace `Tag`, `Note`, `NoteSummary`, drop `EntityType`**

Replace lines 264-294:

```python
class Tag(BaseModel):
    """Flexible label on a contact or company for segmentation.

    Exactly one of ``contact_id`` or ``company_id`` is set (XOR enforced
    at the schema level).
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    name: str
    created_at: datetime


class Note(BaseModel):
    """Freeform text annotation on a contact or company.

    Exactly one of ``contact_id`` or ``company_id`` is set (XOR enforced
    at the schema level).
    """

    id: str
    contact_id: str | None = None
    company_id: str | None = None
    body: str
    created_at: datetime


class NoteSummary(BaseModel):
    """List-view projection of `Note` with truncated body preview."""

    id: str
    contact_id: str | None
    company_id: str | None
    body_preview: str
    created_at: datetime
```

`EntityType` is removed entirely. If anything outside `models.py` imports it, those imports fail -- they will be fixed in subsequent tasks.

- [ ] **Step 11: Run the model tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: the four new tests pass. Other tests in the file may fail temporarily because they reference the old shapes -- those are fixed in this same step or in subsequent slices. Spot-check the failures: any failure in `tests/test_models.py` mentioning `entity_type`, `entity_id`, or old `EnrollmentStatus` values (`pending`/`completed`/`failed`) needs the test updated to the new shape. Update them inline.

- [ ] **Step 12: Run full test suite (expected to have widespread failures)**

Run: `uv run pytest -x 2>&1 | head -80`
Expected: many failures across `test_database.py`, `test_cli.py`, `test_routing.py`, `test_email_ops.py`, `test_sync.py`, `test_agent_tools.py`, `test_agent_invoke.py`. These are the call sites we will fix in slices B and C. Do **not** try to fix them in this task.

- [ ] **Step 13: Commit**

```bash
git add src/mailpilot/models.py tests/test_models.py
git commit -m "refactor(models): nullable FKs on tag/note/activity; collapse EnrollmentStatus"
```

---

## Slice B: Database layer

### Task B1: Tag CRUD with nullable FKs and strict normalization

**Files:**

- Modify: `src/mailpilot/database.py:2103-2280` (Tag section)
- Test: `tests/test_database.py` (Tag tests)

- [ ] **Step 1: Write failing tests for `_normalize_tag_name`**

Add to `tests/test_database.py`:

```python
def test_normalize_tag_name_accepts_valid_inputs() -> None:
    """Lowercase, hyphenated, alphanumeric tags pass through unchanged."""
    from mailpilot.database import _normalize_tag_name

    assert _normalize_tag_name("prospect") == "prospect"
    assert _normalize_tag_name("hot-lead") == "hot-lead"
    assert _normalize_tag_name("q4-2025") == "q4-2025"


def test_normalize_tag_name_collapses_separators_and_case() -> None:
    """Whitespace, underscores, and uppercase are normalized; hyphens collapse."""
    from mailpilot.database import _normalize_tag_name

    assert _normalize_tag_name("Hot Lead") == "hot-lead"
    assert _normalize_tag_name("hot_lead") == "hot-lead"
    assert _normalize_tag_name("HOT--LEAD") == "hot-lead"
    assert _normalize_tag_name("  spaced  ") == "spaced"
    assert _normalize_tag_name("-leading-trailing-") == "leading-trailing"


def test_normalize_tag_name_rejects_invalid() -> None:
    """Names that cannot be normalized to [a-z0-9][a-z0-9-]* raise ValueError."""
    import pytest

    from mailpilot.database import _normalize_tag_name

    with pytest.raises(ValueError):
        _normalize_tag_name("")
    with pytest.raises(ValueError):
        _normalize_tag_name("---")
    with pytest.raises(ValueError):
        _normalize_tag_name("hot/lead")
    with pytest.raises(ValueError):
        _normalize_tag_name("emoji-rocket")  # ok actually
    # Valid sanity:
    assert _normalize_tag_name("emoji-rocket") == "emoji-rocket"
```

(The `emoji-rocket` line is a sanity guard against over-rejecting -- delete the third `with pytest.raises(...)` if it leads to confusion; the regex `[a-z0-9][a-z0-9-]*` does accept it. Keep only the genuine invalid cases above.)

Final form:

```python
def test_normalize_tag_name_rejects_invalid() -> None:
    """Names that cannot be normalized to [a-z0-9][a-z0-9-]* raise ValueError."""
    import pytest

    from mailpilot.database import _normalize_tag_name

    with pytest.raises(ValueError):
        _normalize_tag_name("")
    with pytest.raises(ValueError):
        _normalize_tag_name("---")
    with pytest.raises(ValueError):
        _normalize_tag_name("hot/lead")
    with pytest.raises(ValueError):
        _normalize_tag_name("hot.lead")
```

- [ ] **Step 2: Write failing tests for the new tag CRUD shape**

Replace existing `create_tag` / `delete_tag` / `list_tags` / `search_tags` / `list_entities_by_tag` tests with:

```python
def test_create_contact_tag_and_company_tag(database_connection) -> None:
    """create_tag accepts contact_id XOR company_id."""
    from mailpilot.database import (
        create_company,
        create_contact,
        create_tag,
    )

    company = create_company(database_connection, name="Acme", domain="acme.test")
    contact = create_contact(
        database_connection, email="a@acme.test", first_name="A"
    )

    contact_tag = create_tag(database_connection, contact_id=contact.id, name="prospect")
    assert contact_tag is not None
    assert contact_tag.contact_id == contact.id
    assert contact_tag.company_id is None
    assert contact_tag.name == "prospect"

    company_tag = create_tag(database_connection, company_id=company.id, name="enterprise")
    assert company_tag is not None
    assert company_tag.company_id == company.id
    assert company_tag.contact_id is None


def test_create_tag_requires_exactly_one_owner(database_connection) -> None:
    """contact_id XOR company_id; passing both or neither raises."""
    import pytest

    from mailpilot.database import create_tag

    with pytest.raises(ValueError, match="exactly one"):
        create_tag(database_connection, name="x")
    with pytest.raises(ValueError, match="exactly one"):
        create_tag(database_connection, contact_id="c1", company_id="co1", name="x")


def test_create_tag_normalizes_name(database_connection) -> None:
    """create_tag applies _normalize_tag_name."""
    from mailpilot.database import create_contact, create_tag

    contact = create_contact(database_connection, email="b@acme.test")
    tag = create_tag(database_connection, contact_id=contact.id, name="Hot Lead")
    assert tag is not None
    assert tag.name == "hot-lead"


def test_create_tag_idempotent_on_duplicate(database_connection) -> None:
    """Duplicate insert returns None thanks to ON CONFLICT DO NOTHING."""
    from mailpilot.database import create_contact, create_tag

    contact = create_contact(database_connection, email="c@acme.test")
    first = create_tag(database_connection, contact_id=contact.id, name="prospect")
    second = create_tag(database_connection, contact_id=contact.id, name="prospect")
    assert first is not None
    assert second is None


def test_delete_tag_by_owner(database_connection) -> None:
    from mailpilot.database import create_contact, create_tag, delete_tag

    contact = create_contact(database_connection, email="d@acme.test")
    create_tag(database_connection, contact_id=contact.id, name="cold")
    assert delete_tag(database_connection, contact_id=contact.id, name="cold") is True
    assert delete_tag(database_connection, contact_id=contact.id, name="cold") is False


def test_list_tags_by_owner(database_connection) -> None:
    from mailpilot.database import create_contact, create_tag, list_tags

    contact = create_contact(database_connection, email="e@acme.test")
    create_tag(database_connection, contact_id=contact.id, name="prospect")
    create_tag(database_connection, contact_id=contact.id, name="cold")
    tags = list_tags(database_connection, contact_id=contact.id)
    assert {t.name for t in tags} == {"prospect", "cold"}


def test_list_contacts_by_tag_name(database_connection) -> None:
    from mailpilot.database import create_contact, create_tag, list_contacts_by_tag

    a = create_contact(database_connection, email="x1@acme.test")
    b = create_contact(database_connection, email="x2@acme.test")
    create_tag(database_connection, contact_id=a.id, name="hot")
    create_tag(database_connection, contact_id=b.id, name="hot")
    ids = list_contacts_by_tag(database_connection, name="hot")
    assert set(ids) == {a.id, b.id}


def test_list_companies_by_tag_name(database_connection) -> None:
    from mailpilot.database import (
        create_company,
        create_tag,
        list_companies_by_tag,
    )

    a = create_company(database_connection, name="A", domain="a.test")
    b = create_company(database_connection, name="B", domain="b.test")
    create_tag(database_connection, company_id=a.id, name="enterprise")
    create_tag(database_connection, company_id=b.id, name="enterprise")
    ids = list_companies_by_tag(database_connection, name="enterprise")
    assert set(ids) == {a.id, b.id}
```

- [ ] **Step 3: Run the new tests, expect failures**

Run: `uv run pytest tests/test_database.py -k "tag or normalize" -v`
Expected: all of the above fail with `AttributeError`, `TypeError`, or `ImportError` because the new functions don't exist yet.

- [ ] **Step 4: Add `_normalize_tag_name` and rewrite Tag CRUD in `database.py`**

In `src/mailpilot/database.py`, near the top of the Tag section (around line 2103), add:

```python
import re

_TAG_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")


def _normalize_tag_name(name: str) -> str:
    """Normalize a tag name to lowercase hyphenated form.

    Strips whitespace, lowercases, replaces whitespace and underscores
    with hyphens, collapses repeated hyphens, trims leading/trailing
    hyphens, and validates against ``[a-z0-9][a-z0-9-]*``.

    Raises:
        ValueError: If the result is empty or contains disallowed
        characters.
    """
    cleaned = name.strip().lower()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not _TAG_NAME_RE.fullmatch(cleaned):
        raise ValueError(f"invalid tag name: {name!r} (normalized to {cleaned!r})")
    return cleaned
```

(If `re` is already imported at the top of `database.py`, drop the `import re` line and place `_TAG_NAME_RE` and `_normalize_tag_name` together.)

- [ ] **Step 5: Replace `create_tag`**

Replace the existing `create_tag` function with:

```python
def create_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> Tag | None:
    """Create a tag on a contact or company.

    Exactly one of ``contact_id`` or ``company_id`` must be provided.
    The name is normalized via ``_normalize_tag_name``. Uses ON CONFLICT
    DO NOTHING -- returns None if the tag already exists.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set,
        or if the tag name fails normalization.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    normalized = _normalize_tag_name(name)
    row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "name": normalized,
        },
    ).fetchone()
    connection.commit()
    if row is None:
        return None
    return Tag.model_validate(row)
```

- [ ] **Step 6: Replace `delete_tag`**

```python
def delete_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> bool:
    """Remove a tag from a contact or company.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    normalized = _normalize_tag_name(name)
    if contact_id is not None:
        cursor = connection.execute(
            "DELETE FROM tag WHERE contact_id = %(contact_id)s AND name = %(name)s",
            {"contact_id": contact_id, "name": normalized},
        )
    else:
        cursor = connection.execute(
            "DELETE FROM tag WHERE company_id = %(company_id)s AND name = %(name)s",
            {"company_id": company_id, "name": normalized},
        )
    connection.commit()
    return cursor.rowcount > 0
```

- [ ] **Step 7: Replace `list_tags`**

```python
def list_tags(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[Tag]:
    """List tags on a contact or company.

    Tag has no Summary projection -- the full row already matches the
    summary contract.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if contact_id is not None:
        where_parts.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    else:
        where_parts.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        where_parts.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(where_parts)
    query = SQL("SELECT * FROM tag {} ORDER BY name LIMIT %(limit)s").format(where)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]
```

- [ ] **Step 8: Replace `list_entities_by_tag` with `list_contacts_by_tag` and `list_companies_by_tag`**

Delete `list_entities_by_tag`. Add:

```python
def list_contacts_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
) -> list[str]:
    """Return contact IDs with the given tag (normalized)."""
    normalized = _normalize_tag_name(name)
    rows = connection.execute(
        """\
        SELECT contact_id FROM tag
        WHERE contact_id IS NOT NULL AND name = %(name)s
        ORDER BY created_at
        LIMIT %(limit)s
        """,
        {"name": normalized, "limit": limit},
    ).fetchall()
    return [row["contact_id"] for row in rows]


def list_companies_by_tag(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    limit: int = 100,
) -> list[str]:
    """Return company IDs with the given tag (normalized)."""
    normalized = _normalize_tag_name(name)
    rows = connection.execute(
        """\
        SELECT company_id FROM tag
        WHERE company_id IS NOT NULL AND name = %(name)s
        ORDER BY created_at
        LIMIT %(limit)s
        """,
        {"name": normalized, "limit": limit},
    ).fetchall()
    return [row["company_id"] for row in rows]
```

- [ ] **Step 9: Replace `search_tags`**

```python
def search_tags(
    connection: psycopg.Connection[dict[str, Any]],
    name: str,
    owner: str | None = None,
    limit: int = 100,
) -> list[Tag]:
    """Search tags by name pattern with optional owner filter.

    Args:
        owner: ``"contact"`` to limit to contact tags, ``"company"`` to
            limit to company tags, ``None`` for both.
    """
    if owner not in (None, "contact", "company"):
        raise ValueError("owner must be 'contact', 'company', or None")
    pattern = f"%{name.strip().lower()}%"
    params: dict[str, object] = {"pattern": pattern, "limit": limit}
    owner_filter = SQL("")
    if owner == "contact":
        owner_filter = SQL("AND contact_id IS NOT NULL")
    elif owner == "company":
        owner_filter = SQL("AND company_id IS NOT NULL")
    query = SQL(
        "SELECT * FROM tag WHERE name LIKE %(pattern)s {} "
        "ORDER BY name LIMIT %(limit)s"
    ).format(owner_filter)
    rows = connection.execute(query, params).fetchall()
    return [Tag.model_validate(row) for row in rows]
```

- [ ] **Step 10: Run the tag tests**

Run: `uv run pytest tests/test_database.py -k "tag or normalize" -v`
Expected: PASS for all the tests added in Steps 1 and 2.

- [ ] **Step 11: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "refactor(db): tag CRUD uses nullable FKs; add strict normalization"
```

---

### Task B2: Note CRUD with nullable FKs

**Files:**

- Modify: `src/mailpilot/database.py:2283-` (Note section)
- Test: `tests/test_database.py` (Note tests)

- [ ] **Step 1: Write failing tests for the new note CRUD shape**

Replace existing `create_note` / `list_notes` tests in `tests/test_database.py`:

```python
def test_create_contact_note_and_company_note(database_connection) -> None:
    from mailpilot.database import (
        create_company,
        create_contact,
        create_note,
    )

    contact = create_contact(database_connection, email="n1@acme.test")
    contact_note = create_note(
        database_connection, contact_id=contact.id, body="Met at conf"
    )
    assert contact_note.contact_id == contact.id
    assert contact_note.company_id is None

    company = create_company(database_connection, name="Acme", domain="acme.test")
    company_note = create_note(
        database_connection, company_id=company.id, body="Tier 1 account"
    )
    assert company_note.company_id == company.id
    assert company_note.contact_id is None


def test_create_note_requires_exactly_one_owner(database_connection) -> None:
    import pytest

    from mailpilot.database import create_note

    with pytest.raises(ValueError, match="exactly one"):
        create_note(database_connection, body="x")
    with pytest.raises(ValueError, match="exactly one"):
        create_note(
            database_connection, contact_id="c1", company_id="co1", body="x"
        )


def test_list_notes_by_owner(database_connection) -> None:
    from mailpilot.database import (
        create_contact,
        create_note,
        list_notes,
    )

    contact = create_contact(database_connection, email="n2@acme.test")
    create_note(database_connection, contact_id=contact.id, body="first")
    create_note(database_connection, contact_id=contact.id, body="second")
    summaries = list_notes(database_connection, contact_id=contact.id)
    assert len(summaries) == 2
    assert all(s.contact_id == contact.id for s in summaries)
```

- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_database.py -k note -v`
Expected: failures.

- [ ] **Step 3: Replace `create_note`**

```python
def create_note(
    connection: psycopg.Connection[dict[str, Any]],
    body: str,
    contact_id: str | None = None,
    company_id: str | None = None,
) -> Note:
    """Create a freeform note on a contact or company.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "body": body,
        },
    ).fetchone()
    connection.commit()
    return Note.model_validate(row)
```

- [ ] **Step 4: Replace `list_notes`**

```python
def list_notes(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str | None = None,
    company_id: str | None = None,
    limit: int = 100,
    since: str | None = None,
) -> list[NoteSummary]:
    """List notes on a contact or company as summaries with body previews.

    Raises:
        ValueError: If neither or both of contact_id/company_id are set.
    """
    if (contact_id is None) == (company_id is None):
        raise ValueError("exactly one of contact_id or company_id is required")
    params: dict[str, object] = {"limit": limit}
    where_parts: list[Composed | SQL] = []
    if contact_id is not None:
        where_parts.append(SQL("contact_id = %(contact_id)s"))
        params["contact_id"] = contact_id
    else:
        where_parts.append(SQL("company_id = %(company_id)s"))
        params["company_id"] = company_id
    if since is not None:
        where_parts.append(SQL("created_at >= %(since)s"))
        params["since"] = since
    where = SQL("WHERE ") + SQL(" AND ").join(where_parts)
    query = SQL(
        "SELECT id, contact_id, company_id, "
        "CASE WHEN length(body) > 80 THEN substring(body, 1, 80) || '...' "
        "     ELSE body END AS body_preview, "
        "created_at "
        "FROM note {} ORDER BY created_at DESC LIMIT %(limit)s"
    ).format(where)
    rows = connection.execute(query, params).fetchall()
    return [NoteSummary.model_validate(row) for row in rows]
```

(Confirm the existing implementation already does the body-preview computation in SQL. If it instead does it in Python after `SELECT *`, keep that style and just swap the WHERE clauses. Read the current implementation before editing.)

- [ ] **Step 5: Run note tests, expect pass**

Run: `uv run pytest tests/test_database.py -k note -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "refactor(db): note CRUD uses nullable FKs"
```

---

### Task B3: Activity CRUD with structured FK columns and company-only support

**Files:**

- Modify: `src/mailpilot/database.py:2008-2100` (Activity section)
- Test: `tests/test_database.py` (Activity tests)

- [ ] **Step 1: Write failing tests for the new activity shape**

Add to `tests/test_database.py` (replacing equivalent older tests):

```python
def test_create_activity_with_structured_fks(database_connection) -> None:
    """email_id, workflow_id, task_id are first-class FK columns (#102 sugg 5)."""
    from mailpilot.database import (
        create_account,
        create_activity,
        create_contact,
        create_email,
        create_workflow,
    )

    account = create_account(
        database_connection, email="op@example.test", display_name="Op"
    )
    contact = create_contact(database_connection, email="ct@example.test")
    workflow = create_workflow(
        database_connection,
        account_id=account.id,
        type="outbound",
        name="Test WF",
    )
    email = create_email(
        database_connection,
        account_id=account.id,
        contact_id=contact.id,
        direction="outbound",
        subject="Hi",
        body_text="hi",
    )
    assert email is not None

    activity = create_activity(
        database_connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary="Hi",
        email_id=email.id,
        workflow_id=workflow.id,
    )
    assert activity.email_id == email.id
    assert activity.workflow_id == workflow.id
    assert activity.task_id is None


def test_create_activity_company_only(database_connection) -> None:
    """contact_id is nullable when company_id is provided (#102 sugg 2)."""
    from mailpilot.database import create_activity, create_company

    company = create_company(database_connection, name="Acme", domain="acme.test")
    activity = create_activity(
        database_connection,
        company_id=company.id,
        activity_type="note_added",
        summary="Company note",
    )
    assert activity.contact_id is None
    assert activity.company_id == company.id


def test_create_activity_requires_contact_or_company(database_connection) -> None:
    import pytest

    from mailpilot.database import create_activity

    with pytest.raises(ValueError, match="contact_id or company_id"):
        create_activity(
            database_connection,
            activity_type="note_added",
            summary="orphan",
        )
```

- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_database.py -k activity -v`
Expected: failures.

- [ ] **Step 3: Replace `create_activity`**

```python
def create_activity(
    connection: psycopg.Connection[dict[str, Any]],
    activity_type: str,
    summary: str = "",
    detail: dict[str, object] | None = None,
    contact_id: str | None = None,
    company_id: str | None = None,
    email_id: str | None = None,
    workflow_id: str | None = None,
    task_id: str | None = None,
) -> Activity:
    """Create an activity event.

    At least one of ``contact_id`` or ``company_id`` must be set.
    Structured FK columns (``email_id``, ``workflow_id``, ``task_id``)
    let reports join activity to source records without parsing
    ``detail`` JSON.

    Raises:
        ValueError: If neither contact_id nor company_id is provided.
    """
    if contact_id is None and company_id is None:
        raise ValueError("at least one of contact_id or company_id is required")
    row = connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, email_id, workflow_id, task_id,
            type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s, %(email_id)s,
            %(workflow_id)s, %(task_id)s,
            %(type)s, %(summary)s, %(detail)s
        )
        RETURNING *
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": company_id,
            "email_id": email_id,
            "workflow_id": workflow_id,
            "task_id": task_id,
            "type": activity_type,
            "summary": summary,
            "detail": Json(detail or {}),
        },
    ).fetchone()
    connection.commit()
    return Activity.model_validate(row)
```

- [ ] **Step 4: `list_activities` -- no signature change required**

The existing `list_activities` already accepts `contact_id` and `company_id` filters and returns `ActivitySummary`. The summary now has a nullable `contact_id` (already handled by the model change in A2). No code change here unless the SQL projection lists columns that no longer match -- if so, update the `SELECT` clause to match the new `ActivitySummary` shape:

```python
"SELECT id, contact_id, company_id, type, summary, created_at "
"FROM activity {} ORDER BY created_at DESC LIMIT %(limit)s"
```

(Verify line 2095-2098. The existing projection already matches.)

- [ ] **Step 5: Run activity tests, expect pass**

Run: `uv run pytest tests/test_database.py -k activity -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "refactor(db): activity supports company-only and structured FK columns"
```

---

### Task B4: Atomic helpers for tag/note + activity writes

**Files:**

- Modify: `src/mailpilot/database.py` (Tag and Note sections, after the basic CRUD)
- Test: `tests/test_database.py`

These helpers combine the tag/note insert and the corresponding activity insert in a single transaction, addressing suggestion #4.

- [ ] **Step 1: Write failing tests for the atomic helpers**

Add to `tests/test_database.py`:

```python
def test_add_contact_tag_emits_activity_atomically(database_connection) -> None:
    """add_contact_tag writes tag + tag_added activity in one transaction."""
    from mailpilot.database import (
        add_contact_tag,
        create_contact,
        list_activities,
        list_tags,
    )

    contact = create_contact(database_connection, email="atomic@acme.test")
    tag = add_contact_tag(database_connection, contact_id=contact.id, name="prospect")
    assert tag is not None
    assert tag.name == "prospect"
    assert [t.name for t in list_tags(database_connection, contact_id=contact.id)] == [
        "prospect"
    ]
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].summary == "Tagged as prospect"


def test_add_contact_tag_returns_none_on_duplicate_no_activity(
    database_connection,
) -> None:
    """Duplicate tag insert returns None and emits no activity."""
    from mailpilot.database import (
        add_contact_tag,
        create_contact,
        list_activities,
    )

    contact = create_contact(database_connection, email="dup@acme.test")
    add_contact_tag(database_connection, contact_id=contact.id, name="prospect")
    second = add_contact_tag(
        database_connection, contact_id=contact.id, name="prospect"
    )
    assert second is None
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1  # only the first tag's activity


def test_remove_contact_tag_emits_activity_atomically(database_connection) -> None:
    from mailpilot.database import (
        add_contact_tag,
        create_contact,
        list_activities,
        remove_contact_tag,
    )

    contact = create_contact(database_connection, email="rm@acme.test")
    add_contact_tag(database_connection, contact_id=contact.id, name="cold")
    assert (
        remove_contact_tag(database_connection, contact_id=contact.id, name="cold")
        is True
    )
    types = [a.type for a in list_activities(database_connection, contact_id=contact.id)]
    assert "tag_removed" in types


def test_add_company_tag_emits_company_activity(database_connection) -> None:
    from mailpilot.database import (
        add_company_tag,
        create_company,
        list_activities,
    )

    company = create_company(database_connection, name="Acme", domain="acme.test")
    add_company_tag(database_connection, company_id=company.id, name="enterprise")
    activities = list_activities(database_connection, company_id=company.id)
    assert len(activities) == 1
    assert activities[0].type == "tag_added"
    assert activities[0].company_id == company.id
    assert activities[0].contact_id is None


def test_add_contact_note_emits_activity_atomically(database_connection) -> None:
    from mailpilot.database import (
        add_contact_note,
        create_contact,
        list_activities,
        list_notes,
    )

    contact = create_contact(database_connection, email="note@acme.test")
    note = add_contact_note(
        database_connection, contact_id=contact.id, body="quick note"
    )
    notes = list_notes(database_connection, contact_id=contact.id)
    assert [n.id for n in notes] == [note.id]
    activities = list_activities(database_connection, contact_id=contact.id)
    assert len(activities) == 1
    assert activities[0].type == "note_added"


def test_add_company_note_emits_company_activity(database_connection) -> None:
    from mailpilot.database import (
        add_company_note,
        create_company,
        list_activities,
    )

    company = create_company(database_connection, name="Acme", domain="acme.test")
    add_company_note(database_connection, company_id=company.id, body="ent")
    activities = list_activities(database_connection, company_id=company.id)
    assert len(activities) == 1
    assert activities[0].type == "note_added"
    assert activities[0].company_id == company.id
```

- [ ] **Step 2: Run tests, expect failures (functions don't exist)**

Run: `uv run pytest tests/test_database.py -k "add_contact or add_company or remove_contact or remove_company" -v`
Expected: failures with `AttributeError` or `ImportError`.

- [ ] **Step 3: Implement `add_contact_tag`**

In the Tag section of `database.py`, after `create_tag`:

```python
def add_contact_tag(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    name: str,
) -> Tag | None:
    """Add a tag to a contact and emit a `tag_added` activity atomically.

    The two writes share one transaction. Returns ``None`` if the tag
    already exists -- in that case no activity is written.
    """
    normalized = _normalize_tag_name(name)
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    tag_row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, %(contact_id)s, NULL, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "contact_id": contact_id, "name": normalized},
    ).fetchone()
    if tag_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Tagged as {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return Tag.model_validate(tag_row)
```

- [ ] **Step 4: Implement `add_company_tag`**

```python
def add_company_tag(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    name: str,
) -> Tag | None:
    """Add a tag to a company and emit a `tag_added` company activity atomically."""
    normalized = _normalize_tag_name(name)
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    tag_row = connection.execute(
        """\
        INSERT INTO tag (id, contact_id, company_id, name)
        VALUES (%(id)s, NULL, %(company_id)s, %(name)s)
        ON CONFLICT DO NOTHING
        RETURNING *
        """,
        {"id": _new_id(), "company_id": company_id, "name": normalized},
    ).fetchone()
    if tag_row is None:
        connection.commit()
        return None
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Tagged as {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return Tag.model_validate(tag_row)
```

- [ ] **Step 5: Implement `remove_contact_tag` and `remove_company_tag`**

```python
def remove_contact_tag(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    name: str,
) -> bool:
    """Remove a tag from a contact and emit a `tag_removed` activity atomically."""
    normalized = _normalize_tag_name(name)
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    cursor = connection.execute(
        "DELETE FROM tag WHERE contact_id = %s AND name = %s",
        (contact_id, normalized),
    )
    if cursor.rowcount == 0:
        connection.commit()
        return False
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": f"Removed tag {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return True


def remove_company_tag(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    name: str,
) -> bool:
    """Remove a tag from a company and emit a `tag_removed` activity atomically."""
    normalized = _normalize_tag_name(name)
    cursor = connection.execute(
        "DELETE FROM tag WHERE company_id = %s AND name = %s",
        (company_id, normalized),
    )
    if cursor.rowcount == 0:
        connection.commit()
        return False
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'tag_removed', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": f"Removed tag {normalized}",
            "detail": Json({"tag": normalized}),
        },
    )
    connection.commit()
    return True
```

- [ ] **Step 6: Implement `add_contact_note` and `add_company_note`**

In the Note section of `database.py`, after `create_note`:

```python
def add_contact_note(
    connection: psycopg.Connection[dict[str, Any]],
    contact_id: str,
    body: str,
) -> Note:
    """Add a note to a contact and emit a `note_added` activity atomically."""
    contact_row = connection.execute(
        "SELECT company_id FROM contact WHERE id = %s", (contact_id,)
    ).fetchone()
    if contact_row is None:
        raise ValueError(f"contact not found: {contact_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, %(contact_id)s, NULL, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "contact_id": contact_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, %(contact_id)s, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "contact_id": contact_id,
            "company_id": contact_row["company_id"],
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note


def add_company_note(
    connection: psycopg.Connection[dict[str, Any]],
    company_id: str,
    body: str,
) -> Note:
    """Add a note to a company and emit a `note_added` company activity atomically."""
    if (
        connection.execute(
            "SELECT 1 FROM company WHERE id = %s", (company_id,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"company not found: {company_id}")
    note_row = connection.execute(
        """\
        INSERT INTO note (id, contact_id, company_id, body)
        VALUES (%(id)s, NULL, %(company_id)s, %(body)s)
        RETURNING *
        """,
        {"id": _new_id(), "company_id": company_id, "body": body},
    ).fetchone()
    note = Note.model_validate(note_row)
    connection.execute(
        """\
        INSERT INTO activity (
            id, contact_id, company_id, type, summary, detail
        )
        VALUES (
            %(id)s, NULL, %(company_id)s,
            'note_added', %(summary)s, %(detail)s
        )
        """,
        {
            "id": _new_id(),
            "company_id": company_id,
            "summary": "Note added",
            "detail": Json({"note_id": note.id}),
        },
    )
    connection.commit()
    return note
```

- [ ] **Step 7: Run atomic-helper tests, expect pass**

Run: `uv run pytest tests/test_database.py -k "add_contact or add_company or remove_contact or remove_company" -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mailpilot/database.py tests/test_database.py
git commit -m "feat(db): atomic helpers for tag/note + activity writes"
```

---

### Task B5: Drop pending-state activation in enrollment update

**Files:**

- Modify: `src/mailpilot/database.py:1144-1170` (`list_enrollments`), and any test fixture that defaulted enrollment status to `'pending'`
- Test: `tests/test_database.py`

The schema default is now `'active'`. `list_enrollments` already accepts an optional `status` filter -- the only change is that callers can no longer pass `'pending'`/`'completed'`/`'failed'`.

- [ ] **Step 1: Update enrollment-status tests**

In `tests/test_database.py`, find any test that asserts `enrollment.status == "pending"` after `create_enrollment` and update it:

```python
def test_create_enrollment_defaults_to_active(database_connection) -> None:
    """Enrollment defaults to 'active' (status collapse, comment #4334976677)."""
    from mailpilot.database import (
        create_account,
        create_contact,
        create_enrollment,
        create_workflow,
    )

    account = create_account(
        database_connection, email="op2@example.test", display_name="Op"
    )
    workflow = create_workflow(
        database_connection,
        account_id=account.id,
        type="outbound",
        name="Test WF B5",
    )
    contact = create_contact(database_connection, email="b5@acme.test")

    enrollment = create_enrollment(
        database_connection, workflow_id=workflow.id, contact_id=contact.id
    )
    assert enrollment is not None
    assert enrollment.status == "active"
```

Find any tests asserting `enrollment.status == "completed"` after `update_enrollment`. Replace with assertions that `update_enrollment` only allows `'active'`/`'paused'` (the schema CHECK enforces this; the test verifies the call raises a `psycopg.errors.CheckViolation` or equivalent on bad input).

```python
def test_update_enrollment_rejects_legacy_statuses(database_connection) -> None:
    """`completed`/`failed`/`pending` are no longer valid enrollment statuses."""
    import psycopg

    from mailpilot.database import (
        create_account,
        create_contact,
        create_enrollment,
        create_workflow,
        update_enrollment,
    )

    account = create_account(
        database_connection, email="op3@example.test", display_name="Op"
    )
    workflow = create_workflow(
        database_connection,
        account_id=account.id,
        type="outbound",
        name="Test WF B5b",
    )
    contact = create_contact(database_connection, email="b5b@acme.test")
    create_enrollment(
        database_connection, workflow_id=workflow.id, contact_id=contact.id
    )

    for bad in ("pending", "completed", "failed"):
        with pytest.raises((psycopg.errors.CheckViolation, ValueError)):
            update_enrollment(
                database_connection,
                workflow.id,
                contact.id,
                status=bad,
            )
        database_connection.rollback()
```

(`pytest` import may already be at the top of the file; if not, add it.)

- [ ] **Step 2: Run enrollment tests**

Run: `uv run pytest tests/test_database.py -k enrollment -v`
Expected: PASS, given the schema CHECK from Task A1.

- [ ] **Step 3: Commit**

```bash
git add tests/test_database.py
git commit -m "test(db): enrollment defaults to active; legacy statuses rejected"
```

---

## Slice C: Callers (routing, sync, email_ops, agent, CLI)

### Task C1: Update routing to emit `enrollment_added` with workflow FK

**Files:**

- Modify: `src/mailpilot/routing.py:316-340` (`_ensure_enrollment`)
- Test: `tests/test_routing.py`

- [ ] **Step 1: Update the routing test**

Find the test in `tests/test_routing.py` that asserts a `workflow_assigned` activity is created. Replace `workflow_assigned` with `enrollment_added` and add an assertion that `workflow_id` is populated as a column (not just inside `detail`):

```python
def test_route_email_emits_enrollment_added_activity(...) -> None:
    ...
    activities = list_activities(connection, contact_id=contact.id)
    assert any(
        a.type == "enrollment_added" for a in activities
    )
    full = get_activity(connection, activities[0].id) if False else None  # see note
    # If list_activities returns summaries, hydrate via a direct SELECT:
    row = connection.execute(
        "SELECT workflow_id FROM activity WHERE type = 'enrollment_added' "
        "AND contact_id = %s",
        (contact.id,),
    ).fetchone()
    assert row is not None
    assert row["workflow_id"] == workflow.id
```

(Adjust the test to whatever fixture/setup already exists. The minimum required change is renaming the activity type and asserting `workflow_id` is present in the row.)

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_routing.py -k enrollment -v`
Expected: FAIL because routing still emits `workflow_assigned` and doesn't pass `workflow_id` to `create_activity`.

- [ ] **Step 3: Update `_ensure_enrollment` in `routing.py`**

Replace lines 316-340:

```python
def _ensure_enrollment(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
) -> None:
    """Create an enrollment entry if not already present.

    Emits an ``enrollment_added`` activity only on the initial insert --
    ``create_enrollment`` returns ``None`` on ON CONFLICT so re-routes
    in the same thread do not duplicate the timeline entry.
    """
    enrollment = create_enrollment(connection, workflow_id, contact_id)
    if enrollment is None:
        return
    workflow = get_workflow(connection, workflow_id)
    contact = get_contact(connection, contact_id)
    workflow_name = workflow.name if workflow is not None else ""
    create_activity(
        connection,
        contact_id=contact_id,
        activity_type="enrollment_added",
        summary=f"Assigned to {workflow_name or 'workflow'}",
        detail={"workflow_name": workflow_name},
        company_id=contact.company_id if contact is not None else None,
        workflow_id=workflow_id,
    )
```

`workflow_id` moves out of `detail` and into the structured FK column.

- [ ] **Step 4: Run routing tests, expect pass**

Run: `uv run pytest tests/test_routing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mailpilot/routing.py tests/test_routing.py
git commit -m "refactor(routing): emit enrollment_added activity with workflow FK"
```

---

### Task C2: Drop pending-state activation in `email_ops.py`; pass FKs in `email_sent` activity

**Files:**

- Modify: `src/mailpilot/email_ops.py:76-109`
- Test: `tests/test_email_ops.py`

- [ ] **Step 1: Update email_ops tests**

In `tests/test_email_ops.py`:

1. Delete any test that asserts `enrollment.status` transitions from `"pending"` to `"active"` on send (this state no longer exists).
2. Update the `email_sent` activity test to assert `email_id` and `workflow_id` are present as FK columns:

```python
def test_send_email_emits_email_sent_activity_with_fks(...) -> None:
    ...
    row = connection.execute(
        "SELECT contact_id, email_id, workflow_id FROM activity "
        "WHERE type = 'email_sent' AND contact_id = %s",
        (contact.id,),
    ).fetchone()
    assert row is not None
    assert row["email_id"] == email.id
    assert row["workflow_id"] == workflow.id
```

- [ ] **Step 2: Run, expect failures**

Run: `uv run pytest tests/test_email_ops.py -v`
Expected: failures because of the dropped pending behavior and missing FK columns.

- [ ] **Step 3: Remove `_activate_enrollment_if_pending` and update `_emit_email_sent_activity`**

In `src/mailpilot/email_ops.py`:

1. Delete the `_activate_enrollment_if_pending` function entirely (lines 76-89).
2. Remove every call site of `_activate_enrollment_if_pending` in this file -- the call disappears completely; sending no longer touches enrollment status.
3. Replace `_emit_email_sent_activity` with:

```python
def _emit_email_sent_activity(
    connection: psycopg.Connection[dict[str, Any]],
    contact: Contact,
    email: Email,
    workflow_id: str | None,
) -> None:
    database.create_activity(
        connection,
        contact_id=contact.id,
        activity_type="email_sent",
        summary=email.subject,
        detail={"subject": email.subject},
        company_id=contact.company_id,
        email_id=email.id,
        workflow_id=workflow_id,
    )
```

`email_id` and `workflow_id` move out of `detail` into structured columns.

- [ ] **Step 4: Run email_ops tests, expect pass**

Run: `uv run pytest tests/test_email_ops.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mailpilot/email_ops.py tests/test_email_ops.py
git commit -m "refactor(email_ops): drop pending-state activation; add structured activity FKs"
```

---

### Task C3: Pass `email_id` FK in `email_received` activity (sync.py)

**Files:**

- Modify: `src/mailpilot/sync.py:777-784`
- Test: `tests/test_sync.py`

- [ ] **Step 1: Update sync test**

In `tests/test_sync.py`, find the test asserting `email_received` activity creation and add an FK column assertion:

```python
row = connection.execute(
    "SELECT email_id FROM activity "
    "WHERE type = 'email_received' AND contact_id = %s",
    (contact.id,),
).fetchone()
assert row is not None
assert row["email_id"] == stored_email.id
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_sync.py -k received -v`
Expected: FAIL.

- [ ] **Step 3: Update the call site**

Replace lines 777-784 in `src/mailpilot/sync.py`:

```python
    create_activity(
        connection,
        contact_id=contact.id,
        activity_type="email_received",
        summary=email.subject,
        detail={"subject": email.subject},
        company_id=contact.company_id,
        email_id=email.id,
    )
```

- [ ] **Step 4: Run sync tests, expect pass**

Run: `uv run pytest tests/test_sync.py -k received -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mailpilot/sync.py tests/test_sync.py
git commit -m "refactor(sync): pass email_id FK in email_received activity"
```

---

### Task C4: Replace `update_enrollment_status` agent tool with `record_enrollment_outcome`

**Files:**

- Modify: `src/mailpilot/agent/tools.py:175-222`
- Modify: `src/mailpilot/agent/invoke.py:181-310`
- Test: `tests/test_agent_tools.py`, `tests/test_agent_invoke.py`

- [ ] **Step 1: Write failing tests for `record_enrollment_outcome`**

In `tests/test_agent_tools.py`, replace the existing `update_enrollment_status` tests with:

```python
def test_record_enrollment_outcome_completed_writes_activity_only(
    database_connection,
) -> None:
    """outcome='completed' emits enrollment_completed activity, no status change."""
    from mailpilot.agent.tools import record_enrollment_outcome
    from mailpilot.database import (
        create_account,
        create_contact,
        create_enrollment,
        create_workflow,
        get_enrollment,
        list_activities,
    )

    account = create_account(
        database_connection, email="op4@example.test", display_name="Op"
    )
    workflow = create_workflow(
        database_connection,
        account_id=account.id,
        type="outbound",
        name="C4 WF",
    )
    contact = create_contact(database_connection, email="c4@acme.test")
    create_enrollment(database_connection, workflow.id, contact.id)

    result = record_enrollment_outcome(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="completed",
        reason="meeting booked",
    )
    assert result == {"outcome": "completed", "reason": "meeting booked"}

    enrollment = get_enrollment(database_connection, workflow.id, contact.id)
    assert enrollment is not None
    assert enrollment.status == "active"  # unchanged

    activities = list_activities(database_connection, contact_id=contact.id)
    types = [a.type for a in activities]
    assert "enrollment_completed" in types


def test_record_enrollment_outcome_failed(database_connection) -> None:
    """outcome='failed' emits enrollment_failed activity."""
    from mailpilot.agent.tools import record_enrollment_outcome
    from mailpilot.database import (
        create_account,
        create_contact,
        create_enrollment,
        create_workflow,
        list_activities,
    )

    account = create_account(
        database_connection, email="op5@example.test", display_name="Op"
    )
    workflow = create_workflow(
        database_connection,
        account_id=account.id,
        type="outbound",
        name="C4 WF B",
    )
    contact = create_contact(database_connection, email="c4b@acme.test")
    create_enrollment(database_connection, workflow.id, contact.id)

    record_enrollment_outcome(
        database_connection,
        workflow_id=workflow.id,
        contact_id=contact.id,
        outcome="failed",
        reason="no response",
    )
    types = [
        a.type for a in list_activities(database_connection, contact_id=contact.id)
    ]
    assert "enrollment_failed" in types


def test_record_enrollment_outcome_rejects_invalid_outcome(database_connection) -> None:
    from mailpilot.agent.tools import record_enrollment_outcome

    result = record_enrollment_outcome(
        database_connection,
        workflow_id="wf1",
        contact_id="c1",
        outcome="cancelled",
        reason="x",
    )
    assert result.get("error") == "invalid_outcome"


def test_record_enrollment_outcome_missing_enrollment(database_connection) -> None:
    from mailpilot.agent.tools import record_enrollment_outcome

    result = record_enrollment_outcome(
        database_connection,
        workflow_id="wf-nonexistent",
        contact_id="c-nonexistent",
        outcome="completed",
        reason="x",
    )
    assert result.get("error") == "not_found"
```

- [ ] **Step 2: Run, expect failure**

Run: `uv run pytest tests/test_agent_tools.py -k record_enrollment_outcome -v`
Expected: ImportError (function doesn't exist).

- [ ] **Step 3: Replace the tool implementation in `agent/tools.py`**

Replace `update_enrollment_status` (lines 175-222) with:

```python
def record_enrollment_outcome(
    connection: psycopg.Connection[dict[str, Any]],
    workflow_id: str,
    contact_id: str,
    outcome: str,
    reason: str,
) -> dict[str, str]:
    """Record an outcome (completed or failed) on the activity timeline.

    Outcome is purely a timeline event -- the enrollment row's status is
    not modified. The agent declares the engagement done; if a later
    inbound reply arrives, the agent can react without first
    "reactivating" anything.

    Args:
        connection: Open database connection.
        workflow_id: Current workflow FK.
        contact_id: Contact ID.
        outcome: "completed" or "failed".
        reason: Agent's explanation (e.g., "meeting booked", "no response").

    Returns:
        Dict with the recorded outcome, or an error dict if the
        enrollment is missing or the outcome is invalid.
    """
    valid_outcomes = ("completed", "failed")
    if outcome not in valid_outcomes:
        return {
            "error": "invalid_outcome",
            "message": f"outcome must be one of {valid_outcomes}, got: {outcome}",
        }
    enrollment = database.get_enrollment(connection, workflow_id, contact_id)
    if enrollment is None:
        return {
            "error": "not_found",
            "message": f"enrollment not found: {workflow_id}/{contact_id}",
        }
    contact = database.get_contact(connection, contact_id)
    database.create_activity(
        connection,
        contact_id=contact_id,
        activity_type=f"enrollment_{outcome}",
        summary=reason or f"Enrollment {outcome}",
        detail={"reason": reason},
        company_id=contact.company_id if contact is not None else None,
        workflow_id=workflow_id,
    )
    return {"outcome": outcome, "reason": reason}
```

Also update the module docstring (lines 12-22) to replace `update_enrollment_status` with `record_enrollment_outcome`.

- [ ] **Step 4: Update `agent/invoke.py` wrapper, registration, and prompt**

In `src/mailpilot/agent/invoke.py`:

1. Rename `_wrap_update_enrollment_status` -> `_wrap_record_enrollment_outcome`. Update its body to call `agent_tools.record_enrollment_outcome` with `outcome` instead of `status`:

```python
def _wrap_record_enrollment_outcome(
    ctx: RunContext[AgentDeps], outcome: str, reason: str
) -> dict[str, str]:
    """Record an enrollment outcome (completed or failed) on the timeline."""
    return agent_tools.record_enrollment_outcome(
        ctx.deps.connection,
        workflow_id=ctx.deps.workflow_id,
        contact_id=ctx.deps.contact_id,
        outcome=outcome,
        reason=reason,
    )
```

2. Update the `_TOOLS` registration:

```python
Tool(_wrap_record_enrollment_outcome, name="record_enrollment_outcome"),
```

3. Update `_SYSTEM_PREFIX` -- the existing line that mentions `update_enrollment_status with status='completed'` becomes:

```python
"record_enrollment_outcome with outcome='completed' and a brief reason.\n\n"
```

- [ ] **Step 5: Update `tests/test_agent_invoke.py`**

Find any test asserting that `update_enrollment_status` is registered or that the system prompt mentions it. Replace with `record_enrollment_outcome`. Run: `grep -n update_enrollment_status tests/test_agent_invoke.py` to find the exact lines.

- [ ] **Step 6: Run agent tests, expect pass**

Run: `uv run pytest tests/test_agent_tools.py tests/test_agent_invoke.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/agent/tools.py src/mailpilot/agent/invoke.py \
        tests/test_agent_tools.py tests/test_agent_invoke.py
git commit -m "refactor(agent): replace update_enrollment_status with record_enrollment_outcome"
```

---

### Task C5: CLI tag commands use atomic helpers

**Files:**

- Modify: `src/mailpilot/cli.py:1086-1254` (tag group)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Update CLI tag tests**

In `tests/test_cli.py`, find tag-related tests. Update patches:

- Replace `mailpilot.database.create_tag` patches with `mailpilot.database.add_contact_tag` and `mailpilot.database.add_company_tag`.
- Replace `mailpilot.database.delete_tag` patches with `remove_contact_tag` / `remove_company_tag`.
- Existing tests asserting that `create_activity` is also called separately should be removed -- the activity is now part of the atomic helper.

Add a new test for invalid tag names:

```python
def test_tag_add_rejects_invalid_name(runner, patch_database) -> None:
    result = runner.invoke(cli.main, ["tag", "add", "--contact-id", "c1", "hot/lead"])
    assert result.exit_code != 0
    assert "invalid tag" in result.output.lower()
```

- [ ] **Step 2: Rewrite the `tag add` CLI command**

Replace lines 1086-1143 in `src/mailpilot/cli.py`:

```python
@tag.command("add")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
def tag_add(contact_id: str | None, company_id: str | None, name: str) -> None:
    """Add a tag to a contact or company."""
    from mailpilot.database import (
        add_company_tag,
        add_contact_tag,
        get_company,
        get_contact,
        initialize_database,
    )

    if not name.strip():
        output_error("tag name cannot be empty", "validation_error")
    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            try:
                created = add_contact_tag(connection, contact_id=contact_id, name=name)
            except ValueError as exc:
                output_error(str(exc), "validation_error")
            owner = ("contact", contact_id)
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            try:
                created = add_company_tag(connection, company_id=company_id, name=name)
            except ValueError as exc:
                output_error(str(exc), "validation_error")
            owner = ("company", company_id)
        if created is None:
            from mailpilot.database import _normalize_tag_name

            normalized = _normalize_tag_name(name)
            output_error(
                f"tag '{normalized}' already exists on {owner[0]} {owner[1]}",
                "already_exists",
            )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 3: Rewrite the `tag remove` CLI command**

Replace lines 1146-1193:

```python
@tag.command("remove")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.argument("name")
def tag_remove(contact_id: str | None, company_id: str | None, name: str) -> None:
    """Remove a tag from a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        remove_company_tag,
        remove_contact_tag,
    )

    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            try:
                deleted = remove_contact_tag(
                    connection, contact_id=contact_id, name=name
                )
            except ValueError as exc:
                output_error(str(exc), "validation_error")
            owner = ("contact", contact_id)
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            try:
                deleted = remove_company_tag(
                    connection, company_id=company_id, name=name
                )
            except ValueError as exc:
                output_error(str(exc), "validation_error")
            owner = ("company", company_id)
        from mailpilot.database import _normalize_tag_name

        normalized = _normalize_tag_name(name)
        if not deleted:
            output_error(
                f"tag '{normalized}' not found on {owner[0]} {owner[1]}",
                "not_found",
            )
        output({"removed": True, "tag": normalized, "owner_type": owner[0]})
    finally:
        connection.close()
```

- [ ] **Step 4: Update `tag list` and `tag search`**

Replace `entity_type` / `entity_id` plumbing in `tag list` (lines 1196-1232):

```python
@tag.command("list")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--limit", default=100, help="Maximum results.")
@click.option("--since", default=None, help="ISO datetime lower bound on created_at.")
def tag_list(
    contact_id: str | None,
    company_id: str | None,
    limit: int,
    since: str | None,
) -> None:
    """List tags on a contact or company."""
    from mailpilot.database import (
        get_company,
        get_contact,
        initialize_database,
        list_tags,
    )

    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            tags = list_tags(
                connection, contact_id=contact_id, limit=limit, since=since
            )
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            tags = list_tags(
                connection, company_id=company_id, limit=limit, since=since
            )
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()
```

Update `tag search` to map `--type` -> `owner` parameter on `search_tags`:

```python
@tag.command("search")
@click.argument("name")
@click.option(
    "--type",
    "owner",
    default=None,
    type=click.Choice(["contact", "company"]),
    help="Filter by owner type.",
)
@click.option("--limit", default=100, help="Maximum results.")
def tag_search(name: str, owner: str | None, limit: int) -> None:
    """Search tags by name."""
    from mailpilot.database import initialize_database, search_tags

    connection = initialize_database(_database_url())
    try:
        tags = search_tags(connection, name=name, owner=owner, limit=limit)
        output({"tags": [t.model_dump(mode="json") for t in tags]})
    finally:
        connection.close()
```

- [ ] **Step 5: Delete the now-unused `_resolve_entity` helper**

If `_resolve_entity` (lines around 1075-1083) is no longer referenced after the changes above, delete it. Run `grep -n _resolve_entity src/mailpilot/cli.py` to confirm; if note commands still reference it, leave it for now and remove in Task C6.

- [ ] **Step 6: Run CLI tag tests**

Run: `uv run pytest tests/test_cli.py -k tag -v`
Expected: PASS (with patches updated in Step 1).

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "refactor(cli): tag commands use atomic helpers; XOR contact/company"
```

---

### Task C6: CLI note commands use atomic helpers

**Files:**

- Modify: `src/mailpilot/cli.py:1265-1316` (`note add`) and `note list` block

- [ ] **Step 1: Update CLI note tests**

In `tests/test_cli.py`, update patches: `create_note` -> `add_contact_note`/`add_company_note`. Drop assertions that `create_activity` is called separately for note adds.

- [ ] **Step 2: Rewrite `note add`**

Replace lines 1265-1316 in `src/mailpilot/cli.py`:

```python
@note.command("add")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Company ID.")
@click.option("--body", required=True, help="Note text.")
def note_add(contact_id: str | None, company_id: str | None, body: str) -> None:
    """Add a note to a contact or company."""
    from mailpilot.database import (
        add_company_note,
        add_contact_note,
        get_company,
        get_contact,
        initialize_database,
    )

    if not body.strip():
        output_error("note body cannot be empty", "validation_error")
    if (contact_id is None) == (company_id is None):
        output_error(
            "exactly one of --contact-id or --company-id is required",
            "validation_error",
        )
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None:
            if get_contact(connection, contact_id) is None:
                output_error(f"contact {contact_id} not found", "not_found")
            created = add_contact_note(
                connection, contact_id=contact_id, body=body
            )
        else:
            assert company_id is not None
            if get_company(connection, company_id) is None:
                output_error(f"company {company_id} not found", "not_found")
            created = add_company_note(
                connection, company_id=company_id, body=body
            )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 3: Rewrite `note list`**

Find the existing `note list` command (around line 1319+) and replace its `_resolve_entity` plumbing with the same XOR pattern, calling `list_notes(connection, contact_id=...)` or `list_notes(connection, company_id=...)`.

- [ ] **Step 4: Delete `_resolve_entity` if unused**

After `tag` and `note` no longer call it, delete `_resolve_entity`. Run `grep -n _resolve_entity src/mailpilot/cli.py`; if zero matches, delete the function.

- [ ] **Step 5: Run CLI note tests**

Run: `uv run pytest tests/test_cli.py -k note -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "refactor(cli): note commands use atomic helpers; XOR contact/company"
```

---

### Task C7: CLI activity commands -- new vocabulary, company-only support

**Files:**

- Modify: `src/mailpilot/cli.py:18-30` (`_ACTIVITY_TYPES`), `:962-1014` (`activity create`)

- [ ] **Step 1: Update `_ACTIVITY_TYPES` constant**

Replace lines 18-30 (the `_ACTIVITY_TYPES` tuple) with:

```python
_ACTIVITY_TYPES = (
    "email_sent",
    "email_received",
    "note_added",
    "tag_added",
    "tag_removed",
    "status_changed",
    "enrollment_added",
    "enrollment_completed",
    "enrollment_failed",
    "enrollment_paused",
    "enrollment_resumed",
)
```

- [ ] **Step 2: Update CLI activity tests**

In `tests/test_cli.py`, find tests that pass `--type workflow_assigned` etc. to `activity create`. Replace with `enrollment_added`. Add a test for company-only:

```python
def test_activity_create_supports_company_only(runner, patch_database) -> None:
    result = runner.invoke(
        cli.main,
        [
            "activity",
            "create",
            "--company-id",
            "co1",
            "--type",
            "note_added",
            "--summary",
            "Company note",
        ],
    )
    assert result.exit_code == 0
```

- [ ] **Step 3: Rewrite `activity create`**

Replace lines 967-1013:

```python
@activity.command("create")
@click.option("--contact-id", default=None, help="Contact ID.")
@click.option("--company-id", default=None, help="Optional company ID.")
@click.option(
    "--type",
    "activity_type",
    required=True,
    type=click.Choice(_ACTIVITY_TYPES),
    help="Activity type.",
)
@click.option("--summary", required=True, help="One-line description.")
@click.option("--detail", default=None, help="JSON detail payload.")
def activity_create(
    contact_id: str | None,
    company_id: str | None,
    activity_type: str,
    summary: str,
    detail: str | None,
) -> None:
    """Create an activity event. At least one of --contact-id / --company-id."""
    from mailpilot.database import (
        create_activity,
        get_company,
        get_contact,
        initialize_database,
    )

    if not summary.strip():
        output_error("summary cannot be empty", "validation_error")
    if contact_id is None and company_id is None:
        output_error(
            "at least one of --contact-id or --company-id is required",
            "validation_error",
        )
    detail_dict: dict[str, object] = json.loads(detail) if detail else {}
    connection = initialize_database(_database_url())
    try:
        if contact_id is not None and get_contact(connection, contact_id) is None:
            output_error(f"contact not found: {contact_id}", "not_found")
        if company_id is not None and get_company(connection, company_id) is None:
            output_error(f"company not found: {company_id}", "not_found")
        created = create_activity(
            connection,
            contact_id=contact_id,
            company_id=company_id,
            activity_type=activity_type,
            summary=summary,
            detail=detail_dict,
        )
        output(created.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 4: Run CLI activity tests**

Run: `uv run pytest tests/test_cli.py -k activity -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "refactor(cli): activity vocabulary uses enrollment_*; allow company-only create"
```

---

### Task C8: CLI enrollment update -- active/paused only; emit pause/resume activity

**Files:**

- Modify: `src/mailpilot/cli.py:1840-1926` (enrollment list and update)

- [ ] **Step 1: Update CLI enrollment tests**

In `tests/test_cli.py`:

1. Replace any `--status pending|completed|failed` invocations with `--status active|paused`.
2. Update assertions that previously checked `workflow_completed`/`workflow_failed` activity emission via `enrollment update` -- those are gone (the agent records outcomes now). The CLI's `enrollment update` only emits `enrollment_paused` / `enrollment_resumed` activities on transitions.
3. Add tests:

```python
def test_enrollment_update_paused_emits_activity(runner, patch_database) -> None:
    ...
    # invoke enrollment update --status paused
    # assert enrollment_paused activity present


def test_enrollment_update_active_emits_resumed_activity(runner, patch_database) -> None:
    ...
    # paused -> active emits enrollment_resumed
```

- [ ] **Step 2: Rewrite `enrollment list` status choice**

Replace the `--status` choice at line 1843-1848 with:

```python
@click.option(
    "--status",
    default=None,
    type=click.Choice(["active", "paused"]),
    help="Filter by enrollment status.",
)
```

- [ ] **Step 3: Rewrite `enrollment update`**

Replace lines 1885-1926 with:

```python
@enrollment.command("update")
@click.option("--workflow-id", required=True, help="Workflow ID.")
@click.option("--contact-id", required=True, help="Contact ID.")
@click.option(
    "--status",
    required=True,
    type=click.Choice(["active", "paused"]),
    help="New enrollment status (active or paused).",
)
@click.option("--reason", default=None, help="Status reason.")
def enrollment_update(
    workflow_id: str, contact_id: str, status: str, reason: str | None
) -> None:
    """Update enrollment operational status (active or paused).

    Outcomes (completed, failed) are recorded as activity by the agent
    via record_enrollment_outcome -- not via this command.
    """
    from mailpilot.database import (
        create_activity,
        get_contact,
        get_enrollment,
        initialize_database,
        update_enrollment,
    )

    connection = initialize_database(_database_url())
    try:
        before = get_enrollment(connection, workflow_id, contact_id)
        if before is None:
            output_error("enrollment not found", "not_found")
        fields: dict[str, object] = {"status": status}
        if reason is not None:
            fields["reason"] = reason
        updated = update_enrollment(connection, workflow_id, contact_id, **fields)
        if updated is None:
            output_error("enrollment not found", "not_found")
        if before.status != status:
            contact = get_contact(connection, contact_id)
            activity_type = (
                "enrollment_paused" if status == "paused" else "enrollment_resumed"
            )
            create_activity(
                connection,
                contact_id=contact_id,
                activity_type=activity_type,
                summary=reason or f"Enrollment {status}",
                detail={"reason": reason or ""},
                company_id=contact.company_id if contact is not None else None,
                workflow_id=workflow_id,
            )
        output(updated.model_dump(mode="json"))
    finally:
        connection.close()
```

- [ ] **Step 4: Run CLI enrollment tests**

Run: `uv run pytest tests/test_cli.py -k enrollment -v`
Expected: PASS.

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest -v 2>&1 | tail -30`
Expected: 0 failures. If any remain, they're stragglers from the earlier slices -- fix in this commit.

- [ ] **Step 6: Run lint and types**

Run: `uv run ruff format && uv run ruff check --fix && uv run basedpyright`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/mailpilot/cli.py tests/test_cli.py
git commit -m "refactor(cli): enrollment update accepts active|paused only"
```

---

## Slice D: ADR rewrite, CLAUDE.md, and verification

### Task D1: Rewrite `docs/adr-08-crm-design.md`

**Files:**

- Modify: `docs/adr-08-crm-design.md`

- [ ] **Step 1: Rewrite the ADR body**

Replace the existing content with the structure below. Keep the existing H1 (`# ADR-08: CRM Design -- Activity Timeline, Tags, and Notes`) and Status sections.

```markdown
## Status

Accepted.

## Context

MailPilot is a CRM with Gmail as the primary communication channel. Claude Code is the strategic orchestrator and primary operator; an internal Pydantic AI agent handles real-time tactical work within workflows.

To support relationship reporting, segmentation, and freeform annotation, the schema includes three tables beyond the core (account, company, contact, workflow, enrollment, email, task, sync_status):

- `activity` -- unified per-contact and per-company timeline.
- `tag` -- flexible label on contacts or companies.
- `note` -- freeform text annotation on contacts or companies.

## Decision

### Identity

MailPilot is a CRM. Gmail is the channel. The CLI is the operator surface for Claude Code.

### `activity` -- typed FKs, contact-or-company

Every significant event is an immutable row. Either `contact_id` or `company_id` (or both) is set. Structured FK columns -- `email_id`, `workflow_id`, `task_id` -- let reports join activity to source records without parsing JSON. `detail` JSONB remains for type-specific metadata that does not need to be queryable as a column.

Composite indexes `idx_activity_contact_timeline (contact_id, created_at DESC)` and `idx_activity_company_timeline (company_id, created_at DESC)` serve the common "newest-first timeline for this entity" query.

Activity vocabulary:

- `email_sent`, `email_received` -- send/receive on the Gmail channel.
- `note_added` -- note created on contact or company.
- `tag_added`, `tag_removed` -- tag mutations.
- `status_changed` -- contact-level lifecycle (bounced, unsubscribed).
- `enrollment_added` -- contact joined a workflow.
- `enrollment_completed`, `enrollment_failed` -- agent-declared outcomes (timeline-only, not state on enrollment).
- `enrollment_paused`, `enrollment_resumed` -- operator pause/resume.

### `tag` -- typed nullable FKs (no polymorphism)

`tag.contact_id` and `tag.company_id` are both nullable. A CHECK constraint enforces XOR (exactly one is set). Two partial unique indexes on `(contact_id, name)` and `(company_id, name)` prevent duplicates within an owner.

Tag names are normalized to lowercase, hyphenated form via `_normalize_tag_name`: whitespace and underscores collapse to single hyphens, repeated hyphens collapse, leading/trailing hyphens are trimmed. The result must match `[a-z0-9][a-z0-9-]*`. Names that fail validation raise `ValueError` -- there is no silent salvage.

### `note` -- typed nullable FKs

Same XOR pattern as `tag`. Notes are append-only.

### `enrollment.status` -- operational only

States: `active` (agent considers this contact when the workflow runs) and `paused` (operator/agent has suspended). Outcomes (`completed`, `failed`) live entirely in the activity timeline as `enrollment_completed` / `enrollment_failed` events. The relationship persists across declared outcomes -- a late inbound reply does not require a state transition.

### Atomic timeline writes

Combined helpers in `database.py` write the domain row and its activity in a single transaction:

- `add_contact_tag` / `add_company_tag` / `remove_contact_tag` / `remove_company_tag`
- `add_contact_note` / `add_company_note`

CLI commands and other callers use these helpers instead of two separate writes.

### Activity creation in runtime paths

| Trigger                          | Module                                       | Activity                                                        |
| -------------------------------- | -------------------------------------------- | --------------------------------------------------------------- |
| Outbound send                    | `email_ops.py`                               | `email_sent` (with `email_id`, `workflow_id` FKs)               |
| Inbound store                    | `sync.py`                                    | `email_received` (with `email_id` FK)                           |
| Routing -> enrollment created    | `routing.py`                                 | `enrollment_added` (with `workflow_id` FK)                      |
| Agent declares outcome           | `agent/tools.py` `record_enrollment_outcome` | `enrollment_completed` / `enrollment_failed`                    |
| CLI `enrollment update`          | `cli.py`                                     | `enrollment_paused` / `enrollment_resumed` (on transition only) |
| CLI `tag add/remove`, `note add` | `cli.py` via atomic helper                   | `tag_added` / `tag_removed` / `note_added`                      |

## Consequences

### Positive

- One unified timeline for relationship reporting, joinable to source records via real FKs.
- Maximum segmentation flexibility through tags; no premature commitment to pipeline stages.
- Database-level referential integrity on every CRM relationship (no orphans from polymorphic FKs).
- Simple enrollment state -- two states, no terminal/reactivation problem; outcomes are timeline events.
- Atomic timeline writes prevent CRM/timeline divergence on partial failures.
- Strict tag normalization prevents operational drift (`Hot Lead`, `hot_lead`, `hot--lead` collapse to a single canonical form).

### Negative

- "Which enrollments completed?" requires querying the activity timeline rather than filtering enrollment.status. Acceptable at solo-consultant volume; can be optimized with a materialized view if it becomes a bottleneck.
- Activity table grows unboundedly. Acceptable for current volume; if it exceeds 100K rows, add retention or archival.
- Tag normalization rejects some inputs that the previous lenient `strip().lower()` would have silently accepted. CLI surfaces a `validation_error` so the operator can correct.
```

(Replace existing body. Keep top-level H1 and Status sections.)

- [ ] **Step 2: Commit**

```bash
git add docs/adr-08-crm-design.md
git commit -m "docs(adr): rewrite ADR-08 to match implemented CRM design"
```

---

### Task D2: Update `CLAUDE.md`

**Files:**

- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the CRM Model section**

Find the `### CRM Model` section in `CLAUDE.md`. Update the paragraphs about Tag, Note, Activity, and add a paragraph about enrollment status:

```markdown
- **Contact** -- a person with an email address. May belong to a company.
- **Company** -- an organization identified by domain.
- **Tag** -- flexible label on a contact or company. Each row sets exactly one of `contact_id` or `company_id` (XOR). Tag names are strictly normalized to `[a-z0-9][a-z0-9-]*`; invalid names raise. Convention: lowercase, hyphenated (e.g., `prospect`, `logistics`, `cold`). No formal pipeline stages -- tags are freeform.
- **Note** -- freeform text annotation on a contact or company. Same XOR pattern as Tag. Append-only.
- **Activity** -- chronological event log per contact and/or company. At least one of `contact_id` / `company_id` must be set. Structured FK columns (`email_id`, `workflow_id`, `task_id`) let reports join without parsing `detail` JSON. Activities are append-only.
- **Enrollment** -- contact's binding to a workflow. Status is operational state only: `active` (agent runs against it) or `paused`. Outcomes (`completed`, `failed`) are recorded as activity events via the agent's `record_enrollment_outcome` tool, not as enrollment state.
- **Workflow** -- binds an account to agent instructions for email communication (inbound or outbound). Unchanged from prior design (see `docs/adr-03-workflow-model.md`).
```

- [ ] **Step 2: Update the CLI command reference**

Find `mailpilot enrollment update` in the CLI block. Replace its options block with:

```
mailpilot enrollment update --workflow-id ID --contact-id ID --status active|paused [--reason R]
```

(Drop `pending`, `completed`, `failed` from the help text. The agent's outcome path is documented separately if needed.)

Find `mailpilot activity create` and update to reflect company-only support:

```
mailpilot activity create [--contact-id ID] [--company-id ID] --type TYPE --summary TEXT [--detail JSON]
```

(At least one of `--contact-id` / `--company-id` is required.)

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): reflect ADR-08 refinements in project instructions"
```

---

### Task D3: End-to-end verification

- [ ] **Step 1: Reset both DBs cleanly**

```bash
make clean
DATABASE_URL=postgresql://localhost/mailpilot_test \
    uv run python -c "from mailpilot.database import initialize_database; initialize_database('postgresql://localhost/mailpilot_test').close()"
```

Expected: no error.

- [ ] **Step 2: Run the full check**

```bash
make check
```

Expected: lint + types + tests all green.

- [ ] **Step 3: Run the smoke test**

Invoke the `/smoke-test` skill (see `.claude/skills/smoke-test/SKILL.md`). It exercises the full agent loop against `outbound@lab5.ca` <-> `inbound@lab5.ca`. Verify:

- An outbound `email_sent` activity exists for the test contact, with `email_id` and `workflow_id` set as columns (not just inside `detail`).
- An inbound `email_received` activity exists, with `email_id` set.
- An `enrollment_added` activity was emitted on the first route, with `workflow_id` set.
- If the agent declares an outcome, an `enrollment_completed` or `enrollment_failed` activity exists; the enrollment row's status remains `active`.
- Tag/note add operations on a test contact create the domain row and the corresponding activity in one transaction (no orphan tag/note without an activity).

Verify by running:

```bash
mailpilot activity list --contact-id <TEST_CONTACT_ID> --limit 50
```

- [ ] **Step 4: Open the PR**

Use `/github-pr-create` with issue 102 referenced. The PR title should be along the lines of `refactor(crm): apply ADR-08 refinements (#102)`. Body should list each suggestion (1-8) plus the enrollment-status collapse, marking each as implemented.

---

## Self-Review

**Spec coverage** (each of #102's eight suggestions plus the enrollment collapse):

1. Polymorphic tag/note -> nullable FKs: A1 (schema), A2 (models), B1 (tag CRUD), B2 (note CRUD), C5 (CLI tag), C6 (CLI note).
2. Company-only activity rows: A1 (schema), A2 (Activity model), B3 (CRUD), C7 (CLI).
3. Rename `workflow_*` -> `enrollment_*`: A1 (schema CHECK), A2 (`ActivityType`), C1 (routing), C4 (agent), C7 (CLI), C8 (CLI).
4. Atomic timeline writes: B4 (helpers), C5/C6 (CLI uses helpers).
5. Structured FK columns on activity: A1 (schema), A2 (model), B3 (CRUD), C1/C2/C3/C4/C8 (callers pass FKs).
6. Composite timeline indexes: A1.
7. Strict tag normalization: B1 (regex + helper).
8. Update ADR-08 to reflect implemented behavior: file rename done; D1 rewrites content; D2 updates CLAUDE.md.

Plus enrollment-status collapse from comment #4334976677: A1 (schema), A2 (`EnrollmentStatus`), B5 (tests), C2 (drop `_activate_enrollment_if_pending`), C4 (agent tool replacement), C8 (CLI).

**Placeholder scan:** No `TBD`, `TODO`, "implement later", or "similar to Task N" without code. Each step that changes code shows the code. Bash commands include expected output where relevant.

**Type consistency:**

- `Tag.contact_id` / `Tag.company_id`, `Note.contact_id` / `Note.company_id`, `Activity.contact_id` / `Activity.company_id`, `Activity.email_id` / `Activity.workflow_id` / `Activity.task_id` -- consistent across A2, B1-B4, C1-C7.
- `EnrollmentStatus = Literal["active", "paused"]` consistent across A2, B5, C8.
- New tool name: `record_enrollment_outcome(connection, workflow_id, contact_id, outcome, reason)` consistent across C4 (tools), C4 (invoke wrapper), C4 (tests), C4 (system prompt).
- New helper names: `add_contact_tag`, `add_company_tag`, `remove_contact_tag`, `remove_company_tag`, `add_contact_note`, `add_company_note`, `list_contacts_by_tag`, `list_companies_by_tag` -- consistent across B1, B4, C5, C6.

No gaps detected.
