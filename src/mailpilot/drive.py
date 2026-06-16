"""Google Drive API client for reading Markdown KB files.

Authentication: same service account + domain-wide delegation as
:mod:`mailpilot.gmail`. Per-account impersonation via ``with_subject``.

Scope: ``https://www.googleapis.com/auth/drive.readonly`` (read-only).

Used by the agent tools ``list_drive_markdown``, ``read_drive_markdown``,
and ``search_drive_markdown`` to ground inbound auto-replies in
operator-curated Markdown documents.
"""

from __future__ import annotations

from typing import Any

DriveService = Any
"""Type alias for the Drive API service resource (untyped by Google)."""

_DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]

_MARKDOWN_MIME_TYPE = "text/markdown"

_SEARCH_TOKEN_CAP = 8
"""Upper bound on the number of ``fullText contains`` predicates a single
search query emits (`§V.106`). Caps query length so a verbose question does
not balloon into an unwieldy predicate list; the most salient leading tokens
still surface the file."""

_DRIVE_HTTP_TIMEOUT_SECONDS = 60
"""Socket-level timeout on the Drive transport so a hung TCP read
surfaces as ``socket.timeout`` quickly. The bounded auto-retry
classifier (``mailpilot.agent.retry.is_transient``) treats those as
transient per `§V.49`."""


def build_drive_service(email: str) -> DriveService:
    """Build a Drive API service instance with delegated credentials.

    The transport is wrapped with ``httplib2.Http(timeout=...)`` so a
    hung TCP read on Drive surfaces as ``socket.timeout`` within the
    bound. Per `§V.49`, those timeouts feed the bounded auto-retry
    classifier; the bound also caps how long a single Drive call can
    stall the agent's read window.

    Args:
        email: Gmail address to impersonate via domain-wide delegation.

    Returns:
        Drive API service resource.
    """
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    from mailpilot.gmail import build_delegated_credentials

    delegated = build_delegated_credentials(_DRIVE_SCOPE, email)
    authed_http = AuthorizedHttp(
        delegated,
        http=httplib2.Http(timeout=_DRIVE_HTTP_TIMEOUT_SECONDS),
    )
    return build("drive", "v3", http=authed_http)


def _build_search_tokens(query: str) -> list[str]:
    """Tokenize a free-text query into Drive ``fullText`` search tokens.

    Splits the query on whitespace. For any hyphenated token the raw token is
    retained whole AND its hyphen-split parts are added, so a model code like
    ``DM42-Q-FRP`` is searched both whole and as ``DM42`` / ``Q`` / ``FRP``
    (`§V.106`). This avoids the false-negative class of `§B.89`, where Drive
    punctuation-tokenizes and AND-joins a whole-phrase predicate. Order is
    preserved, tokens are de-duplicated, and the result is capped at
    :data:`_SEARCH_TOKEN_CAP`.

    Args:
        query: Free-text search query.

    Returns:
        De-duplicated, order-preserving list of search tokens (possibly empty
        when the query has no non-whitespace content).
    """
    tokens: list[str] = []
    seen: set[str] = set()

    def _add(token: str) -> None:
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)

    for raw_token in query.split():
        _add(raw_token)
        if "-" in raw_token:
            for part in raw_token.split("-"):
                _add(part)
    return tokens[:_SEARCH_TOKEN_CAP]


class DriveClient:
    """Thin wrapper around the Drive v3 service for KB reads.

    Holds the impersonated service for one account. Construction triggers
    the credentials build; tests should use :meth:`from_service`.

    Usage::

        client = DriveClient("user@example.com")
        files = client.list_markdown(folder_id)
        body = client.read_markdown(files[0]["file_id"])
        hits = client.search_markdown(folder_id, "shipping policy")
    """

    def __init__(self, email: str) -> None:
        self.email = email
        self._service: DriveService = build_drive_service(email)

    @classmethod
    def from_service(cls, email: str, service: DriveService) -> DriveClient:
        """Create a client with a pre-built service (for testing)."""
        client = cls.__new__(cls)
        client.email = email
        client._service = service
        return client

    def list_markdown(self, folder_id: str) -> list[dict[str, str]]:
        """List Markdown files in a Drive folder.

        Args:
            folder_id: Drive folder ID.

        Returns:
            List of ``{"file_id": ..., "name": ...}``.
        """
        query = (
            f"mimeType='{_MARKDOWN_MIME_TYPE}' "
            f"and parents in '{folder_id}' "
            f"and trashed = false"
        )
        response: dict[str, Any] = (
            self._service.files()
            .list(
                q=query,
                fields="files(id, name)",
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files: list[dict[str, str]] = []
        for entry in response.get("files", []):
            files.append({"file_id": entry["id"], "name": entry["name"]})
        return files

    def search_markdown(self, folder_id: str, query: str) -> list[dict[str, str]]:
        """Full-text search Markdown files in a Drive folder.

        Wraps Drive ``files.list`` with an OR-joined set of per-token
        ``fullText contains`` predicates scoped to the folder (`§V.106`). The
        query is whitespace-tokenized and hyphenated tokens are tried both
        whole and split (see :func:`_build_search_tokens`), so a single salient
        term surfaces the file and a hyphenated model code in a generically
        named datasheet is not stranded by Drive's whole-phrase punctuation
        tokenization (`§B.89`). Results are unioned and de-duplicated by
        ``file_id``, preserving Drive's native relevance order.

        Args:
            folder_id: Drive folder ID.
            query: Free-text search query. Drive matches against file
                content and metadata.

        Returns:
            List of ``{"file_id": ..., "name": ...}`` ranked by Drive's
            native relevance, de-duplicated by ``file_id``. Empty list when no
            document matches or the query has no searchable tokens.
        """
        tokens = _build_search_tokens(query)
        if not tokens:
            return []
        predicates = " or ".join(f"fullText contains '{token}'" for token in tokens)
        drive_query = (
            f"mimeType='{_MARKDOWN_MIME_TYPE}' "
            f"and '{folder_id}' in parents "
            f"and ({predicates}) "
            f"and trashed = false"
        )
        response: dict[str, Any] = (
            self._service.files()
            .list(
                q=drive_query,
                fields="files(id, name)",
                corpora="allDrives",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files: list[dict[str, str]] = []
        seen_file_ids: set[str] = set()
        for entry in response.get("files", []):
            file_id = entry["id"]
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            files.append({"file_id": file_id, "name": entry["name"]})
        return files

    def read_markdown(self, file_id: str) -> dict[str, str]:
        """Read the content of a Markdown file from Drive.

        Args:
            file_id: Drive file ID.

        Returns:
            ``{"name": ..., "content": ..., "web_view_link": ...}``.
        """
        metadata: dict[str, Any] = (
            self._service.files()
            .get(
                fileId=file_id,
                fields="name, webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        media: bytes = (
            self._service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )
        return {
            "name": metadata.get("name", ""),
            "content": media.decode("utf-8", errors="replace"),
            "web_view_link": metadata.get("webViewLink", ""),
        }
