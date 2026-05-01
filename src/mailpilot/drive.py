"""Google Drive API client for reading Markdown KB files.

Authentication: same service account + domain-wide delegation as
:mod:`mailpilot.gmail`. Per-account impersonation via ``with_subject``.

Scope: ``https://www.googleapis.com/auth/drive.readonly`` (read-only).

Used by the agent tools ``list_drive_markdown`` and ``read_drive_markdown``
to ground inbound auto-replies in operator-curated Markdown documents.
"""

from __future__ import annotations

from typing import Any

DriveService = Any
"""Type alias for the Drive API service resource (untyped by Google)."""

_DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive.readonly"]

_MARKDOWN_MIME_TYPE = "text/markdown"


def build_drive_service(email: str) -> DriveService:
    """Build a Drive API service instance with delegated credentials.

    Args:
        email: Gmail address to impersonate via domain-wide delegation.

    Returns:
        Drive API service resource.
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    from mailpilot.gmail import resolve_credentials_path

    credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        resolve_credentials_path(),
        scopes=_DRIVE_SCOPE,
    )
    delegated = credentials.with_subject(email)
    return build("drive", "v3", credentials=delegated)


class DriveClient:
    """Thin wrapper around the Drive v3 service for KB reads.

    Holds the impersonated service for one account. Construction triggers
    the credentials build; tests should use :meth:`from_service`.

    Usage::

        client = DriveClient("user@example.com")
        files = client.list_markdown(folder_id)
        body = client.read_markdown(files[0]["file_id"])
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
