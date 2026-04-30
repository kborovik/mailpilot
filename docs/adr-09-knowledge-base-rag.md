# ADR-09: Knowledge Base -- Google Drive Markdown Lookup

## Status

Proposed.

## Context

The public demo at https://lab5.ca/demo/ requires the inbound auto-reply agent to answer product questions grounded in company documentation, and to decline politely when the question is outside the knowledge base. See issue #108.

Existing capabilities cover the surrounding loop end-to-end: Pub/Sub triggering, inbound sync, routing, agent invocation with Pydantic AI tools, and Gmail reply (`docs/adr-01-gmail-api-integration.md`, `docs/adr-03-workflow-model.md`, `docs/adr-04-email-routing.md`, `docs/email-flow.md`). The only missing capability is a way for the agent to look up authoritative content during a run.

The default industry pattern is RAG: ingest source documents into a vector store, embed at query time, retrieve top-k chunks, ground the answer. That introduces a vector database (pgvector or external), an embedding provider, an ingestion pipeline, chunking strategy, and re-index logic. For the demo and the foreseeable product scope -- dozens of short product specs per account -- this is more machinery than the problem requires.

## Decision

The knowledge base is a Google Drive folder containing Markdown files. Two agent tools read it directly via the Drive API. No PostgreSQL tables, no embeddings, no vector index, no chunking, no in-house ingestion.

### Source of truth

A single Drive folder per account holds the account's knowledge base as `.md` files. The folder ID is configured per account; how the Markdown got there is out of scope (operators can convert PDFs / Docs / web pages by any means they prefer and drop the result into the folder).

The folder ID lives on the `account` row as a new nullable column `kb_drive_folder_id`. An account without a folder configured has no KB and the search tool returns an empty list.

### Agent tools

Both tools are added to `src/mailpilot/agent/tools.py` and registered in `src/mailpilot/agent/invoke.py`. They follow the existing convention: typed signatures, dependency-injected `connection` / `account` / `gmail_client` (extended for Drive), dict return shapes, error dicts on failure.

```python
def search_drive_markdown(
    drive_client: DriveClient,
    account: Account,
    query: str,
) -> list[dict[str, str]]:
    """Search the account's KB folder for Markdown files matching the query.

    Drive's fullText search is keyword-based. The agent is expected to
    issue several phrasings if a single query returns nothing.

    Returns: [{"file_id": ..., "name": ..., "snippet": ...}]
    """


def read_drive_markdown(
    drive_client: DriveClient,
    account: Account,
    file_id: str,
) -> dict[str, str] | None:
    """Read a Markdown file from the account's KB folder.

    Scoping check: the file must live under `account.kb_drive_folder_id`.
    Returns None if the file is not found or not in the account's folder
    -- prevents prompt-injection-driven cross-account reads.

    Returns: {"name": ..., "content": ..., "web_view_link": ...}
    """
```

`search_drive_markdown` calls `files.list` with:

```
q = "mimeType='text/markdown'
     and parents in '<account.kb_drive_folder_id>'
     and fullText contains '<query>'
     and trashed = false"
fields = "files(id, name)"
```

The snippet returned to the agent is the first ~200 characters of the file (one extra `files.get` per hit). If snippet cost becomes a problem, drop it -- the agent can call `read_drive_markdown` directly.

`read_drive_markdown` calls `files.get(fileId, fields="id, name, parents, webViewLink")` to verify the file is in the configured folder, then `files.export` / `files.get(alt=media)` to fetch content. Files outside the account's folder return `None`.

### Decline behaviour

Prompt-driven. The inbound auto-reply system prompt instructs the agent to call `search_drive_markdown` before answering substantive questions, and to reply with a polite decline if the search returns no relevant hits. No enforcer tool. The agent still satisfies the "must call at least one tool per run" invariant in `agent/invoke.py` because the decline path calls `search_drive_markdown` (which legitimately returns an empty list) and then `reply_email`.

### Drive client

A new `DriveClient` lives in `src/mailpilot/gmail.py` (or a sibling `drive.py`) using the same service account + domain-wide delegation as `GmailClient`. Single scope addition: `https://www.googleapis.com/auth/drive.readonly`. Per-account impersonation via `credentials.with_subject(account.email)`. `AgentDeps` in `src/mailpilot/agent/invoke.py` gains a `drive_client: DriveClient` field.

## Consequences

### Positive

- No schema changes beyond a single column on `account`. No migrations. No new dependencies beyond `google-api-python-client` (already present for Gmail).
- Drive is the source of truth. Operators edit Markdown in Drive and the agent sees changes immediately -- no re-index step.
- One Google API project, one service account, one auth path. Same operational story as Gmail.
- YAGNI-aligned. Adding embeddings later is straightforward: another tool that wraps a vector store, switched on in the system prompt.

### Negative

- Drive `fullText contains` is keyword-based. Synonyms and paraphrases are the agent's problem: it must try multiple phrasings on a miss. For typical product-spec content this is acceptable; for free-form long-form knowledge it would not be.
- Each agent run that uses the KB makes two-to-many Drive API round-trips. Drive's per-user quota is generous; the per-account impersonation distributes load across users. Not a near-term concern.
- The agent loads full file content into context. This is fine for short product specs (~kilobytes) and would not be fine for book-length sources. If a future use case demands long sources, chunking and embeddings come back on the table.
- No offline / disconnected operation. If Drive is unreachable, the agent cannot answer. The decline path still works (search returns an error -> agent replies with a generic "I can't access our docs right now").

### Cross-tenant safety

`read_drive_markdown` verifies the requested `file_id` is under `account.kb_drive_folder_id` before returning content. Prompt injection in an inbound email body cannot trick the tool into reading another account's documents, mirroring the existing `read_email` account-scoping check.

## Alternatives Considered

### pgvector + embedding pipeline

The default RAG approach: pgvector tables, embedding provider (Voyage / OpenAI), `mailpilot kb sync` ingestion command, chunking, HNSW index. Rejected for v1 -- it solves a problem (semantic search across long-form sources at scale) the demo and current product scope do not have. Adds a dependency, a vendor relationship, ingestion logic, and re-index logic for negligible quality benefit on keyword-rich product specs.

If keyword search proves insufficient in production, the migration path is additive: add the tables and embedding pipeline, register a new `search_indexed_drive_documents` tool, leave the Drive lookup tools in place during the transition.

### In-house PDF/Docs to Markdown conversion

A `mailpilot kb sync` command that walks the Drive folder, converts non-Markdown formats to Markdown via Claude, and writes results back to Drive. Rejected for v1 as out of scope -- the operator handles conversion outside the system. Markdown-only is a deliberate constraint.

### Local file system source

Index a local directory instead of Drive. Rejected: operators already use Drive for collaboration, and per-account impersonation gives natural multi-tenant isolation. A local directory would need its own access-control story.

## Out of Scope

- PDF / Google Docs / HTML to Markdown conversion. Operators handle this externally and drop `.md` files into Drive.
- Embedding-based semantic search.
- Cross-account knowledge sharing.
- Drive change-feed / push-based re-index. Not needed -- there is no index.
- Web UI for KB management. Drive is the UI.
