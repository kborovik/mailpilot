"""Tests for the Drive API client wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock

from mailpilot.drive import DriveClient


def _make_service(
    list_response: dict[str, object] | None = None,
    metadata_response: dict[str, object] | None = None,
    media_response: bytes = b"",
) -> MagicMock:
    """Build a mock Drive service that records list/get calls."""
    service = MagicMock()
    files = service.files.return_value

    list_handle = MagicMock()
    list_handle.execute.return_value = list_response or {"files": []}
    files.list.return_value = list_handle

    get_handle = MagicMock()
    get_handle.execute.return_value = metadata_response or {}
    files.get.return_value = get_handle

    media_handle = MagicMock()
    media_handle.execute.return_value = media_response
    files.get_media.return_value = media_handle

    return service


def test_list_markdown_returns_file_ids_and_names() -> None:
    service = _make_service(
        list_response={
            "files": [
                {"id": "f1", "name": "alpha.md"},
                {"id": "f2", "name": "beta.md"},
            ]
        }
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.list_markdown("FOLDER")

    assert result == [
        {"file_id": "f1", "name": "alpha.md"},
        {"file_id": "f2", "name": "beta.md"},
    ]


def test_list_markdown_query_filters_to_markdown_in_folder_excluding_trash() -> None:
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.list_markdown("FOLDER42")

    call_kwargs = service.files.return_value.list.call_args.kwargs
    query = call_kwargs["q"]
    assert "mimeType='text/markdown'" in query
    assert "parents in 'FOLDER42'" in query
    assert "trashed = false" in query
    assert call_kwargs["fields"] == "files(id, name)"


def test_list_markdown_empty_folder_returns_empty_list() -> None:
    service = _make_service(list_response={"files": []})
    client = DriveClient.from_service("user@example.com", service)

    result = client.list_markdown("EMPTY")

    assert result == []


def test_read_markdown_returns_name_content_and_web_view_link() -> None:
    service = _make_service(
        metadata_response={"name": "guide.md", "webViewLink": "https://x/y"},
        media_response=b"# Guide\n\nHello world.",
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.read_markdown("FILE1")

    assert result == {
        "name": "guide.md",
        "content": "# Guide\n\nHello world.",
        "web_view_link": "https://x/y",
    }


def test_read_markdown_uses_alt_media_for_body() -> None:
    service = _make_service(
        metadata_response={"name": "x.md", "webViewLink": "https://x"},
        media_response=b"body",
    )
    client = DriveClient.from_service("user@example.com", service)

    client.read_markdown("FID")

    files = service.files.return_value
    metadata_kwargs = files.get.call_args.kwargs
    media_kwargs = files.get_media.call_args.kwargs
    assert metadata_kwargs == {"fileId": "FID", "fields": "name, webViewLink"}
    assert media_kwargs == {"fileId": "FID"}


def test_read_markdown_decodes_utf8_with_replacement_on_invalid_bytes() -> None:
    service = _make_service(
        metadata_response={"name": "bad.md", "webViewLink": "https://x"},
        media_response=b"hi \xff there",
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.read_markdown("FID")

    assert result["content"].startswith("hi ")
    assert "there" in result["content"]
