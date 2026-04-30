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
- tool: `read_drive_markdown(file_id: str) -> dict[str,str]`
  - ok → `{"name": ..., "content": ..., "web_view_link": ...}`
  - err → `{"error": ..., "message": ...}`
  - call: `files.get(fileId, alt=media)` for body; `files.get(fileId, fields="name,webViewLink")` for metadata.
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
T12|x|clarify Gate B8 in `.claude/skills/smoke-test/SKILL.md` -- filter by `workflow_id == OUTBOUND_WORKFLOW_ID`, ⊥ by `--account-id`. Why: operator-driven trigger sends from `outbound@` are normal in B; only agent-driven sends from outbound *workflow* are the regression signal|-
T13|x|analyze `pubsub.notification` vs `pubsub.notification.received` span duplication in `src/mailpilot/pubsub.py` (17 of each in last smoke run, 33 spans for ~conceptually-one event). Decision: **collapse** -- drop `pubsub.notification.received` log, add `email` attribute to existing span. Rationale: aligns w/ project convention (every other operation span carries identifier as attribute), 50% record reduction on hot path, no test impact. Failure-path `pubsub.notification.decode_error` log preserved|-
T14|x|apply T13 collapse -- drop `logfire.debug("pubsub.notification.received")` in `src/mailpilot/pubsub.py:213`, set `email` as attribute on `pubsub.notification` span. Add span-contract test: success path emits 1 `pubsub.notification` row w/ `email` attr & no `pubsub.notification.received` row|-

## §B BUGS
id|date|cause|fix
