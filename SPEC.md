# SPEC

## §G GOAL

agent tools ! ground inbound auto-reply in Markdown files from Drive folder named in workflow.instructions.

## §C CONSTRAINTS

- Python 3.14. basedpyright strict. ruff. TDD per CLAUDE.md.
- Reuse service account + domain-wide delegation (same as Gmail).
- One new OAuth scope: `https://www.googleapis.com/auth/drive.readonly`. Read-only.
- No schema changes. No embeddings. No vector store. No ingestion pipeline.
- Folder ID lives in workflow `instructions` (operator-written prompt). ⊥ schema column.
- Multi-tenant isolation ! delegated to Drive permission model. Impersonated user ! has Viewer ≥ on folder.
- Tool pattern follows existing `src/mailpilot/agent/tools.py`: typed sig, DI deps, dict return, error dicts on failure (⊥ raise).
- Operators drop `.md` files in Drive. ⊥ in-app PDF/Docs/HTML conversion.
- Folder cardinality: low (single-digit to low-double-digit files). ∴ list-then-read fine, ⊥ `fullText contains` search.

## §I INTERFACES

- tool: `list_drive_markdown(folder_id: str) -> list[dict[str,str]] | dict[str,str]`
  - ok → `[{"file_id": ..., "name": ...}, ...]`
  - err → `{"error": "drive_unavailable"|"not_found"|..., "message": ...}`
  - q: `mimeType='text/markdown' and parents in '<folder_id>' and trashed = false`
  - fields: `files(id, name)`
  - flags: `corpora="allDrives"` & `supportsAllDrives=True` & `includeItemsFromAllDrives=True`. Why: KB folder may live in Shared Drive; default `files.list` corpora excludes SD children → silent empty result for impersonated SD member.
- tool: `read_drive_markdown(file_id: str) -> dict[str,str]`
  - ok → `{"name": ..., "content": ..., "web_view_link": ...}`
  - err → `{"error": ..., "message": ...}`
  - call: `files.get(fileId, alt=media)` for body; `files.get(fileId, fields="name,webViewLink")` for metadata.
  - flags: `supportsAllDrives=True` on both `files.get` & `files.get_media`. Required for SD files.
- module: `src/mailpilot/drive.py` → `DriveClient` (mirrors `GmailClient` shape).
- deps: `AgentDeps.drive_client: DriveClient` in `src/mailpilot/agent/invoke.py`.
- scope: `drive.readonly` added to service-account scope list.
- prompt: agent system prompt instructs — when workflow.instructions name folder → call `list_drive_markdown` → call `read_drive_markdown` on most relevant → ground reply | polite decline.

## §V INVARIANTS

V1: Drive scope = `drive.readonly` only. ⊥ write/modify scopes.
V2: Drive auth = service account + `credentials.with_subject(account.email)`. Per-account impersonation.
V3: `list_drive_markdown` query ! filter `mimeType='text/markdown' & parents in '<folder_id>' & trashed = false`.
V4: Drive tool failure → return `{"error": ..., "message": ...}` dict. ⊥ raise to agent.
V5: ∀ agent run → ≥1 tool call (existing invariant). Decline path ! call `list_drive_markdown` + `reply_email` ∴ holds.
V6: Folder access = Drive permission of impersonated user. Folder ID ∉ secrets & ∉ access grants.
V7: ∀ new tool → unit tests cover {hit, no-hit, drive-error}.
V8: `make check` ! green (ruff + basedpyright strict + pytest).
V9: ∀ KB file in lab5.ca/demo Drive folder ! explicitly shared w/ `demo@lab5.ca` as user-reader. `anyoneWithLink` perm ⊥ surface files in `files.list(q="parents in 'F'")` for service-account-as-user; only files in user's "Shared with me" view appear. Folder share alone ⊥ propagate to per-file list visibility.
V10: ∀ JSON-yielding CLI command ! emit valid JSON on stdout, ⊥ preceded | interleaved by operator-log lines. Operator-log routes to stderr or follows the JSON envelope. Why: smoke-test parsers fail strict-JSON parse when `event=...` line precedes `{`.
V11: KB folder MAY live in Shared Drive. ∴ `list_drive_markdown` ! set `corpora="allDrives"` & `supportsAllDrives=True` & `includeItemsFromAllDrives=True`. `read_drive_markdown` ! set `supportsAllDrives=True` on `files.get` & `files.get_media`. Why: default `files.list` corpora excludes SD children → silent empty result for impersonated users who are SD members. Supersedes V9 as operative guidance (V9 retained as historical context). Backprop B1.
V12: `agent.invoke` span `trigger` attr ! reflect caller path. Allowed values: `enrollment_run` (CLI manual via `mailpilot enrollment run`), `task` (background drain via `run.execute_task`), `email` (email-driven via routing → task), `manual` (other direct programmatic calls). Why: conflated trigger labels mask operator-initiated retries as task drains, breaking Logfire-based regression detection in smoke tests. Backprop B2.

## §T TASKS

id|status|task|cites
T1|x|add `DriveClient` in `src/mailpilot/drive.py` — auth, list, get-media|V1,V2,I.module
T2|x|wire `drive.readonly` scope in service-account creds path|V1
T3|x|impl `list_drive_markdown` tool in `src/mailpilot/agent/tools.py`|V3,V4,I.tool
T4|x|impl `read_drive_markdown` tool in `src/mailpilot/agent/tools.py`|V4,I.tool
T5|x|extend `AgentDeps` w/ `drive_client` & register tools in `src/mailpilot/agent/invoke.py`|I.deps
T6|x|update agent system prompt — KB grounding + decline behavior|V5,I.prompt
T7|x|unit tests: both tools × {hit, no-hit, drive-error}|V7
T8|x|unit tests: `DriveClient` (list, get, error mapping)|V7
T9|x|smoke-test scenario — KB-grounded reply + polite decline in `.claude/skills/smoke-test/SKILL.md`|V5,V6
T10|x|`make check` clean|V8
T11|x|add `is_routed` to `EmailSummary` & `list_emails` projection -- list rows ! answer routing state w/o `view`. Why: smoke-test gates kept needing `email view <id>` only to check `is_routed` (currently null on summary even when routed)|-
T12|x|clarify Gate B8 in `.claude/skills/smoke-test/SKILL.md` -- filter by `workflow_id == OUTBOUND_WORKFLOW_ID`, ⊥ by `--account-id`. Why: operator-driven trigger sends from `outbound@` are normal in B; only agent-driven sends from outbound _workflow_ are the regression signal|-
T13|x|analyze `pubsub.notification` vs `pubsub.notification.received` span duplication in `src/mailpilot/pubsub.py` (17 of each in last smoke run, 33 spans for ~conceptually-one event). Decision: **collapse** -- drop `pubsub.notification.received` log, add `email` attribute to existing span. Rationale: aligns w/ project convention (every other operation span carries identifier as attribute), 50% record reduction on hot path, no test impact. Failure-path `pubsub.notification.decode_error` log preserved|-
T14|x|apply T13 collapse -- drop `logfire.debug("pubsub.notification.received")` in `src/mailpilot/pubsub.py:213`, set `email` as attribute on `pubsub.notification` span. Add span-contract test: success path emits 1 `pubsub.notification` row w/ `email` attr & no `pubsub.notification.received` row|-
T15|x|migrate KB → Shared Drive `MailPilot` (`0AJIvyECg210LUk9PVA`), new folder `MailPilot Demo` (`1IUuPinOopUv_YWOZyFpt2ZX8Hd8bpZat`); demo@lab5.ca = SD Reader; `anyoneWithLink:reader` on each file for stranger access; update demo workflow `instructions` → new folder ID; add Phase 0 KB-visibility gate in `.claude/skills/smoke-test/SKILL.md` (impersonates `demo@lab5.ca` via `DriveClient.list_markdown`, expects 3 files); add `corpora="allDrives"` + `supportsAllDrives=True` + `includeItemsFromAllDrives=True` flags to `DriveClient` in `src/mailpilot/drive.py` + tests. Why: per V9, `anyoneWithLink` alone ⊥ surface files in list query for impersonated user; SD membership does. Original "user-share each .md" approach replaced by structural SD migration|V9,V11
T16|x|investigated redundant outbound `agent.invoke` during Scenario A smoke run on 2026-04-30. Hypothesis refuted -- ⊥ code path creates task from sync seeing outbound mailbox's own send. `_collect_new_message_ids` filters by `label_id="INBOX"`; `_store_inbound_message` writes `direction="inbound"` only; `create_tasks_for_routed_emails` (database.py:2017) filters `direction='inbound'` & dedupes via `NOT EXISTS (task WHERE email_id=e.id)`. Logfire shows 2 distinct trace_ids for the pre-A7 invocations (19:54:17, 19:54:35), 18s apart, ⊥ wrapped by `run.execute_task` -- both came from `mailpilot enrollment run` CLI. 2nd correctly noop'd via `search_emails` + `noop` after seeing prior send. ⊥ spurious DB task. Decision: **other** → remedy via T18 (CLI trigger label) + T19 (smoke SKILL single-invocation discipline). Skipped optional outbound-side short-circuit (low value, safe behavior)|V12
T17|.|route `operator_event(...)` away from JSON-yielding single-shot CLI commands so stdout stays strict-JSON. Options: route operator_event to stderr globally; or suppress for non-`run` commands; or print after the JSON envelope. Pick one, verify `mailpilot enrollment run | python3 -c "import sys,json; json.load(sys.stdin)"` succeeds on a clean stream. Long-running `mailpilot run` keeps emitting events on stdout (operator console)|V10
T18|.|replace heuristic trigger inference in `src/mailpilot/agent/invoke.py:484` (`trigger="email" if email else ("task" if task_description else "manual")`) w/ explicit caller-passed `trigger: str` arg on `invoke_workflow_agent`. CLI `enrollment run` (cli.py:1767) ! pass `trigger="enrollment_run"`; `run.execute_task` (run.py:117) ! pass `trigger="task"`. Add span-contract test: CLI invocation emits `trigger="enrollment_run"` span attr, background drain emits `trigger="task"`. Why: current heuristic conflates manual CLI runs w/ background task drains -- masks operator double-invocation as regression. Backprop B2|V12
T19|.|tighten `.claude/skills/smoke-test/SKILL.md` A3: `mailpilot enrollment run` ! exactly once per (workflow_id, contact_id). If outbound email ⊥ visible after run, poll `email list --since <TEST_START_A>` -- ⊥ re-invoke `enrollment run`. Update Scenario A Logfire review: regression signal becomes count of `trigger="task"` spans (expect 1 = A7), ⊥ total `agent.invoke`. `trigger="enrollment_run"` spans tolerated regardless of count. Why: backprop T16 -- smoke-runner Claude re-fired `enrollment run` 18s later, producing cosmetically-redundant 3rd `agent.invoke` that reads as regression but is idempotent operator behavior|T18

## §B BUGS

id|date|cause|fix
B1|2026-04-30|Drive `anyoneWithLink: reader` ⊥ surface files in `files.list(q="parents in 'F'")` for service-account-as-`demo@lab5.ca`; only explicit user-share lands in "Shared with me" view. lab5.ca/demo answered as if KB had only the RO file, declined valid SF-100S question|V9,V11
B2|2026-04-30|3rd outbound `agent.invoke` @ 19:54:35 was 2nd `mailpilot enrollment run` CLI invocation, ⊥ task drain. Distinct trace_id from 1st; ⊥ wrapped by `run.execute_task`; agent correctly noop'd after `search_emails` saw prior send. Original hypothesis (sync re-observing outbound mailbox's own send) refuted -- no such path. Real cause: `enrollment run` ⊥ idempotent against operator double-fire & current heuristic `trigger="task"` label conflates CLI invocations w/ task drains, masking the cause|V12
B3|2026-04-30|`event=agent.run ...` operator-log line emitted to stdout before the JSON envelope in `mailpilot enrollment run` → strict `json.load(sys.stdin)` fails. CLI contract per CLAUDE.md says stdout is JSON only|V10
