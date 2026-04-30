# ADR-09: Knowledge Base -- Google Drive Markdown Lookup

## Status

Proposed.

## Context

The public demo at https://lab5.ca/demo/ requires the inbound auto-reply agent to answer product questions grounded in company documentation, and to decline politely when the question is outside the knowledge base. See issue #108.

Existing capabilities cover the surrounding loop end-to-end: Pub/Sub triggering, inbound sync, routing, agent invocation with Pydantic AI tools, and Gmail reply (`docs/adr-01-gmail-api-integration.md`, `docs/adr-03-workflow-model.md`, `docs/adr-04-email-routing.md`, `docs/email-flow.md`). The only missing capability is a way for the agent to look up authoritative content during a run.

The default industry pattern is RAG: ingest source documents into a vector store, embed at query time, retrieve top-k chunks, ground the answer. That introduces a vector database, an embedding provider, an ingestion pipeline, chunking strategy, and re-index logic. For the demo and the foreseeable product scope -- a handful of short product specs per workflow -- this is more machinery than the problem requires.

## Decision

The knowledge base is a Google Drive folder containing Markdown files. The folder ID is supplied in the workflow's `instructions` (the operator-written prompt body). Two agent tools list and read the folder via the Drive API. No PostgreSQL tables, no embeddings, no vector index, no chunking, no in-house ingestion, no schema changes.

### Source of truth and configuration

A workflow's KB is whatever Drive folder the operator names in the workflow's `instructions`. The agent reads the folder ID from its own prompt and passes it to the tools. Different workflows on the same account can point at different folders; a workflow can reference multiple folders if the operator writes them in.

How Markdown got into the folder is out of scope. Operators convert PDFs, Docs, web pages, etc. by any means they prefer and drop the result in.

### Agent tools

Both tools are added to `src/mailpilot/agent/tools.py` and registered in `src/mailpilot/agent/invoke.py`. They follow the existing convention: typed signatures, dependency-injected `drive_client`, dict return shapes, error dicts on failure. The agent supplies `folder_id` / `file_id` from its prompt context.

```python
def list_drive_markdown(
    drive_client: DriveClient,
    folder_id: str,
) -> list[dict[str, str]]:
    """List Markdown files in a Drive folder.

    Returns: [{"file_id": ..., "name": ...}, ...]
    Errors:  {"error": "drive_unavailable" | "not_found" | ..., "message": ...}
    """


def read_drive_markdown(
    drive_client: DriveClient,
    file_id: str,
) -> dict[str, str]:
    """Read the content of a Markdown file from Drive.

    Returns: {"name": ..., "content": ..., "web_view_link": ...}
    Errors:  {"error": "drive_unavailable" | "not_found" | ..., "message": ...}
    """
```

`list_drive_markdown` calls `files.list` with:

```
q = "mimeType='text/markdown'
     and parents in '<folder_id>'
     and trashed = false"
fields = "files(id, name)"
```

`read_drive_markdown` calls `files.get(fileId, alt=media)` and returns the body verbatim. There is no implicit folder-membership check on read -- the workflow's folder ID is operator-supplied, not a security boundary, and the service account's Drive permissions are what gate access (see "Access control" below).

### Decline behaviour

Prompt-driven. The inbound auto-reply system prompt instructs the agent, when the workflow's `instructions` name a Drive folder, to call `list_drive_markdown` and then `read_drive_markdown` on the most relevant file before answering substantive questions, and to reply with a polite decline if no file is relevant. No enforcer tool. The agent still satisfies the "must call at least one tool per run" invariant in `agent/invoke.py` because the decline path calls `list_drive_markdown` (which legitimately surfaces no relevant file) and then `reply_email`.

### Drive client

A new `DriveClient` lives alongside `GmailClient` (sibling `src/mailpilot/drive.py`) using the same service account + domain-wide delegation. Single scope addition: `https://www.googleapis.com/auth/drive.readonly`. Per-account impersonation via `credentials.with_subject(account.email)`. `AgentDeps` in `src/mailpilot/agent/invoke.py` gains a `drive_client: DriveClient` field.

### Access control

Multi-tenant isolation is delegated entirely to Drive's permission model. The service account impersonates the account's user via domain-wide delegation; the impersonated user must have at least Viewer permission on the folder for the tools to return content. A workflow naming a folder its impersonated user cannot see receives a Drive 404 -- the tool surfaces this as `{"error": "not_found", ...}` and the agent replies with the decline path.

This means folder IDs in workflow `instructions` are not secrets, but they are also not access grants. Pasting another tenant's folder ID into a workflow does not enable reading it.

## Consequences

### Positive

- Zero schema change. No migration. No new dependencies beyond `google-api-python-client` (already present for Gmail).
- Drive is the source of truth. Operators edit Markdown in Drive and the agent sees changes immediately -- no re-index step.
- Workflow-scoped KB falls out naturally: each workflow names its folder in `instructions`. No coupling to `account`, no need for a separate `kb_source` table to support multiple KBs per account.
- One Google API project, one service account, one auth path. Same operational story as Gmail.
- YAGNI-aligned. Adding embeddings later is straightforward: another tool that wraps a vector store, switched on in the system prompt.

### Negative

- Listing the whole folder per run scales with folder size. For typical product-spec folders (single-digit to low-double-digit `.md` files) this is fine; for hundreds of files the agent's context cost on the listing alone becomes meaningful.
- The agent loads full file content into context. This is fine for short product specs (~kilobytes) and would not be fine for book-length sources. If a future use case demands long sources, chunking and embeddings come back on the table.
- No semantic match -- the agent picks files by filename and (after reading) by content. Filenames need to be operator-friendly (`pump-flow-rates.md`, not `doc-1.md`).
- No offline / disconnected operation. If Drive is unreachable, the agent cannot answer. The error path still works -- `list_drive_markdown` returns `{"error": "drive_unavailable", ...}` and the agent replies with a generic "I can't access our docs right now".

### Operator responsibility

Folder IDs in `instructions` are operator-controlled prompt content. An operator who pastes the wrong folder ID into a workflow will get wrong-domain answers. This is no different from any other prompt-content mistake (wrong tone instructions, wrong product description) and is not a system concern.

## Alternatives Considered

### pgvector + embedding pipeline

The default RAG approach: pgvector tables, embedding provider (Voyage / OpenAI), `mailpilot kb sync` ingestion command, chunking, HNSW index. Rejected for v1 -- it solves a problem (semantic search across long-form sources at scale) the demo and current product scope do not have. Adds a dependency, a vendor relationship, ingestion logic, and re-index logic for negligible quality benefit on small folders of keyword-rich product specs.

If keyword/listing search proves insufficient in production, the migration path is additive: add the tables and embedding pipeline, register a new `search_indexed_drive_documents` tool, leave the Drive lookup tools in place during the transition.

### Per-account Drive folder column on `account`

An earlier draft of this ADR put a `kb_drive_folder_id` column on `account`. Rejected -- it forces one KB per account, requires schema migration, and duplicates state that already lives naturally in workflow `instructions`. Workflow-scoped is also more flexible: outbound and inbound workflows on the same account can reference the same folder or different folders without any plumbing.

### `fullText contains` Drive search instead of folder listing

Use `files.list` with `fullText contains '<query>'` instead of listing all `.md` files. Rejected for v1 -- listing is simpler for small folders, and Drive's keyword search has quirks (no synonyms, stem-only matching) that would push complexity into the system prompt. Filename-based selection by the LLM is sufficient at current scale.

### In-house PDF / Docs to Markdown conversion

A `mailpilot kb sync` command that walks the Drive folder, converts non-Markdown formats to Markdown via Claude, and writes results back to Drive. Rejected for v1 as out of scope -- the operator handles conversion outside the system. Markdown-only is a deliberate constraint.

### Local file system source

Index a local directory instead of Drive. Rejected: operators already use Drive for collaboration, and per-account impersonation gives natural multi-tenant isolation via Drive permissions. A local directory would need its own access-control story.

## Out of Scope

- PDF / Google Docs / HTML to Markdown conversion. Operators handle this externally and drop `.md` files into Drive.
- Embedding-based semantic search.
- Drive change-feed / push-based re-index. Not needed -- there is no index.
- Web UI for KB management. Drive is the UI.
