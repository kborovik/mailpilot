# Read-only TUI for companies and contacts

## Problem

Operators read companies and contacts through `mailpilot company list` and
`mailpilot contact list` JSON envelopes. JSON on stdout serves scripts, not
humans (§V.3). A human browsing the CRM needs search, navigation, and a detail
pane without composing jq pipelines. Access must stay read-only because company
and contact rows are live, paid data (§V.119).

## Proposal

**Module.** New flat module `src/mailpilot/tui.py` holds one Textual `App`
subclass, `MailpilotTui`. No package split; the repo favors flat modules. Split
only if the module grows past a maintainable size after v1.

**Framework.** Textual as an optional extra `mailpilot-crm[tui]`, not a main
dependency. Agents and scripts never need it. The command lazy-imports Textual
so `cli.py` stays click-only at module level (§V.2). Missing install yields the
standard error envelope with a message that points at `pip install
mailpilot-crm[tui]` (or the uv equivalent) and exit 1.

**Entry.** Top-level `mailpilot tui` sits beside `run` and `status` (§I). The
command refuses to start when stdout is not a TTY and emits the standard error
envelope with exit 1. Escape bytes never reach a pipe, so the JSON contract
stays safe everywhere it applies.

**Read-only, three layers.**

1. The connection opens with `SET SESSION CHARACTERISTICS AS TRANSACTION READ
   ONLY`. The database enforces read-only even when `database_url` carries a
   write-capable user.
2. `tui.py` imports only an allowlisted set of read functions from
   `database.py`. A contract test AST-parses the module and requires every
   `database` import name to sit on that allowlist (no write verbs:
   `create_`, `update_`, `disable_`, `enable_`, `delete_`, provision,
   migrate, export, import).
3. Connect path never calls `initialize_database` auto-provision. Plain open,
   set session read-only, then read-only schema diagnosis. Empty or broken
   schema yields an error envelope and exit 1. The TUI never creates tables.

**Zero new SQL.** List, search, tags, and views go through the same
`database.py` functions the CLI verbs use. No parallel query surface.

**Detail composition.** Core entity fields come from the shared loaders so
TUI, CLI, and agent pre-feed stay field-identical (§V.8, §V.135 spirit):

| Pane | Core (shared loader) | Extra (existing list fns) |
| ---- | -------------------- | ------------------------- |
| Company detail | `load_company_view` | `list_tags(company_id=...)`, `list_contacts(company_id=...)` |
| Contact detail | `load_contact_view` | `list_tags(contact_id=...)` |

`CompanyView` is profile + notes only. Tags and child contacts are composed
beside it, not claimed as view fields. `profile` (`dict`) renders as
pretty-printed JSON in a scrollable block, not a raw `str(dict)` blob.

**Layout (true master-detail).**

- Two tabs: Companies, Contacts.
- Each tab is master-detail: `DataTable` on the left, detail panel on the
  right. Cursor movement updates the right pane immediately. No Enter to open
  the primary detail; no hidden screen stack for the main view.
- Company columns: name, domain, `has_profile`, `contact_count`, and a
  disabled marker when include-disabled is on. Contact columns: name, email,
  `title`, `company_domain`, `email_confidence`, and a disabled marker when
  shown. Both mirror the §I list projections plus the operator-critical
  risk and profile signals.
- Detail load is debounced on cursor motion so rapid navigation does not
  stampede `load_*_view`.
- Company detail shows: core `CompanyView` fields, tags, at most 10 inline
  notes, and the company contact list. Contact detail shows: core
  `ContactView` fields (including company notes when present) and tags.
- Enter on a company contact row focuses that contact in the Contacts tab
  (or loads contact detail in the right pane). From contact detail, Enter on
  `company_domain` (or a bound key) focuses the parent company in the
  Companies tab. Cross-link both ways.
- Escape clears search focus or returns focus to the table. It does not pop a
  detail stack that does not exist.

**Search and disabled.**

- `/` focuses a search input. Submit calls `search_companies` or
  `search_contacts` with the same server semantics as the CLI `search` verb
  (default limit applies; see Data volume).
- `search_*` does not take `include_disabled`. When include-disabled is off,
  the TUI filters search results client-side on `disabled_reason IS NULL`.
  Server results may include disabled rows; the UI hides them unless `d` is
  on.
- Empty search query restores the list path (`list_companies` /
  `list_contacts` with `include_disabled` from the `d` toggle).

**Keys.**

| Key | Action |
| --- | ------ |
| `/` | Focus search |
| `d` | Toggle include-disabled (default off, §V.114) |
| `r` | Refresh current tab list and focused detail |
| `?` | Help overlay with keybindings |
| Enter | Cross-link (company contact row → contact; contact domain → company) |
| Escape | Clear search focus / return focus to table |
| `q` | Quit |

**Status line.** Always shows: tab name, visible row count, truncated flag when
the fetch hit the limit, include-disabled on/off, and the active search query
when set.

**Data volume.** Each tab loads through the existing list functions with an
explicit limit (start at the CLI default of 100, or a single documented higher
cap if operators need it). There is no unlimited list API and no claim of a
full row set. When the result count equals the limit, the status line marks
the table truncated; search is the scale path. Paging stays out until
operators hit the cap in practice (YAGNI).

**Connection.** The TUI reads `settings.database_url` only. Production
browsing is `MAILPILOT_DATABASE_URL=<reporter URL> mailpilot tui` via §V.85
precedence. Load reporter credentials from `.env` the same way as ad-hoc
`psql` (Claude.md production-database section); never paste passwords into the
shell history. The read-only session guard makes even a mistaken write-capable
URL safe against DML; it does not excuse using a write role for browse.

**Testing.**

- Contract: non-TTY refusal (error envelope, exit 1, zero escape bytes).
- Contract: AST allowlist of `database` imports in `tui.py`.
- Contract: core detail fields equal `company view` / `contact view` via the
  shared loaders (field-for-field on the loader output, not the composed
  extras).
- Behavior: Textual `Pilot` headless tests for tab switch, search submit,
  disabled toggle, refresh, and cross-link focus.
- Glue unit tests for client-side disabled filtering and truncated status.

## Alternative considered

Composition of existing pieces was considered instead of building:
`mailpilot company list | jq | fzf --preview 'mailpilot company view {}'`.
Zero new code, but no tabs, no drill-down from a company to its contacts, and
it adds external tool dependencies. Rejected as the primary shape; it remains
a usable interim tool, and stays stronger than the TUI for discover-set
filter jobs until the TUI gains those filters.

## Effect on in-flight SPEC items

- §I gains a top-level `tui` command row plus the optional `textual` extra
  note (`mailpilot-crm[tui]`).
- §V.3 gains a narrow carve-out: `tui` draws to the terminal; non-TTY stdout
  refuses with an error envelope; all other commands stay unchanged.
- New §V candidate (single invariant): TUI is a read-only view over the CLI
  loaders — session `READ ONLY`, no auto-provision, allowlisted read imports
  only, no parallel SQL; test-enforced.
- New §T row covers the implementation. No existing §T row is superseded.

## Design decisions

- **Decision:** Entry is the top-level `mailpilot tui` command with a narrow
  §V.3 carve-out. **Why:** One binary and one settings path; the carve-out is
  one clause, and the non-TTY refusal keeps every pipe JSON-safe.
- **Decision:** `textual` is an optional extra, not a main dependency.
  **Why:** TUI is operator-only; agents and scripts should not pay the install
  weight. Lazy import keeps `cli.py` startup unchanged per §V.2; missing extra
  is a clear error envelope.
- **Decision:** True master-detail with cursor-driven detail, not a list then
  fullscreen stack. **Why:** One navigation model. Tabs handle noun switch;
  cursor handles selection; Enter is reserved for cross-links.
- **Decision:** Tables load with an explicit limit and a truncated status
  marker; no paging in v1. **Why:** Existing list/search APIs are limited
  (default 100). Claiming a full row set would either lie or force new SQL.
  Search and a higher documented cap cover current sizes under YAGNI.
- **Decision:** Detail is composed — shared `load_*_view` plus existing
  `list_tags` / `list_contacts`. **Why:** `CompanyView` does not carry tags or
  child contacts. Composition keeps zero new SQL while matching what operators
  need to see.
- **Decision:** Connect without `initialize_database` provision. **Why:**
  Auto-provision writes schema on an empty DB and breaks the read-only browse
  contract. Diagnosis-only open matches the CLI read path intent.
- **Decision:** Include-disabled for search is client-side filter.
  **Why:** `search_*` has no `include_disabled` flag today; filtering after
  fetch avoids new SQL and keeps `d` honest.
- **Decision:** Production browsing uses the `MAILPILOT_DATABASE_URL`
  environment override only. **Why:** §V.85 precedence already supports it
  with zero new code, and the read-only session guard holds for any URL.

## Success criterion

- `mailpilot tui` with stdout piped emits the §V.4 error envelope and exits 1,
  with zero escape bytes in the pipe.
- Missing `textual` install emits an error envelope that names the `[tui]`
  extra and exits 1.
- Any write attempted over the TUI connection fails at the database.
- Empty-database connect refuses without provisioning.
- Core company and contact detail fields equal `mailpilot company view` and
  `mailpilot contact view` field-for-field through the shared loaders,
  test-enforced. Tags and child contacts are documented extras, not view
  parity claims.
- AST allowlist test rejects any non-read `database` import in `tui.py`.

## Out of scope

- Any mutation from the TUI — notes, tags, and disable stay CLI-owned.
- Other nouns (email, enrollment, task, meeting) — add only on demand.
- Custom theming beyond Textual defaults.
- Discover-set filters (`--has-profile`, `--max-contacts` / `--min-contacts`,
  `--tag` / `--no-tag`). CLI and fzf remain the path for those jobs until a
  later filter chip or modal is justified by operator demand.
- Unlimited list API and paging. Revisit only when the truncated marker
  appears in real use.
- New `search_*` parameters for `include_disabled`. Client-side filter covers
  the TUI; server-side can land later if CLI search needs the same toggle.
