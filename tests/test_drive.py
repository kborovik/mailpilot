"""Tests for the Drive API client wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mailpilot.drive import DriveClient, build_drive_service


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
    assert call_kwargs["corpora"] == "allDrives"
    assert call_kwargs["supportsAllDrives"] is True
    assert call_kwargs["includeItemsFromAllDrives"] is True


def test_list_markdown_empty_folder_returns_empty_list() -> None:
    service = _make_service(list_response={"files": []})
    client = DriveClient.from_service("user@example.com", service)

    result = client.list_markdown("EMPTY")

    assert result == []


def test_search_markdown_returns_file_ids_and_names_in_drive_order() -> None:
    service = _make_service(
        list_response={
            "files": [
                {"id": "f7", "name": "shipping.md"},
                {"id": "f3", "name": "returns.md"},
            ]
        }
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.search_markdown("FOLDER", "shipping policy")

    assert result == [
        {"file_id": "f7", "name": "shipping.md"},
        {"file_id": "f3", "name": "returns.md"},
    ]


def test_search_markdown_query_includes_fulltext_and_folder_with_shared_drive_flags() -> (
    None
):
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.search_markdown("FOLDER42", "shipping policy")

    call_kwargs = service.files.return_value.list.call_args.kwargs
    query = call_kwargs["q"]
    assert "mimeType='text/markdown'" in query
    assert "'FOLDER42' in parents" in query
    assert "trashed = false" in query
    assert call_kwargs["fields"] == "files(id, name)"
    assert call_kwargs["corpora"] == "allDrives"
    assert call_kwargs["supportsAllDrives"] is True
    assert call_kwargs["includeItemsFromAllDrives"] is True


def test_search_markdown_multi_word_query_or_joins_per_token_predicates() -> None:
    """§V.106: a multi-word query never collapses to a single whole-phrase
    ``fullText contains '{query}'`` predicate; each token is its own predicate,
    OR-joined."""
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.search_markdown("FOLDER", "shipping policy")

    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "fullText contains 'shipping'" in query
    assert "fullText contains 'policy'" in query
    assert " or " in query
    # never the whole-phrase predicate that regressed in §B.89
    assert "fullText contains 'shipping policy'" not in query


def test_search_markdown_hyphenated_token_tried_whole_and_split() -> None:
    """§V.106 / §B.89: a hyphenated model code is searched whole AND split, so
    the in-table ``DM42-Q-FRP`` is not stranded by Drive's punctuation
    tokenization."""
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.search_markdown("FOLDER", "DM42-Q-FRP")

    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "fullText contains 'DM42-Q-FRP'" in query
    assert "fullText contains 'DM42'" in query
    assert "fullText contains 'Q'" in query
    assert "fullText contains 'FRP'" in query
    assert " or " in query


def test_search_markdown_single_token_emits_one_predicate() -> None:
    """§V.106: a single salient term surfaces the file via one predicate (no
    spurious OR)."""
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.search_markdown("FOLDER", "datasheet")

    query = service.files.return_value.list.call_args.kwargs["q"]
    assert "fullText contains 'datasheet'" in query
    assert " or " not in query


def test_search_markdown_caps_predicate_count_at_eight_tokens() -> None:
    """§V.106: ~8-token cap bounds query length on a verbose question."""
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    client.search_markdown("FOLDER", "a b c d e f g h i j k")

    query = service.files.return_value.list.call_args.kwargs["q"]
    assert query.count("fullText contains") == 8


def test_search_markdown_unions_and_dedupes_results_by_file_id() -> None:
    """§V.106: per-token matches are unioned and de-duplicated by file_id,
    preserving first-seen (Drive relevance) order."""
    service = _make_service(
        list_response={
            "files": [
                {"id": "f7", "name": "shipping.md"},
                {"id": "f3", "name": "returns.md"},
                {"id": "f7", "name": "shipping.md"},
            ]
        }
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.search_markdown("FOLDER", "shipping returns policy")

    assert result == [
        {"file_id": "f7", "name": "shipping.md"},
        {"file_id": "f3", "name": "returns.md"},
    ]


def test_search_markdown_blank_query_returns_empty_without_api_call() -> None:
    """§V.106: a query with no searchable tokens never emits an invalid empty
    predicate group; it short-circuits to an empty list."""
    service = _make_service()
    client = DriveClient.from_service("user@example.com", service)

    result = client.search_markdown("FOLDER", "   ")

    assert result == []
    service.files.return_value.list.assert_not_called()


def test_search_markdown_no_match_returns_empty_list() -> None:
    service = _make_service(list_response={"files": []})
    client = DriveClient.from_service("user@example.com", service)

    result = client.search_markdown("FOLDER", "nothing matches this")

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
    assert metadata_kwargs == {
        "fileId": "FID",
        "fields": "name, webViewLink",
        "supportsAllDrives": True,
    }
    assert media_kwargs == {"fileId": "FID", "supportsAllDrives": True}


def test_read_markdown_decodes_utf8_with_replacement_on_invalid_bytes() -> None:
    service = _make_service(
        metadata_response={"name": "bad.md", "webViewLink": "https://x"},
        media_response=b"hi \xff there",
    )
    client = DriveClient.from_service("user@example.com", service)

    result = client.read_markdown("FID")

    assert result["content"].startswith("hi ")
    assert "there" in result["content"]


def test_build_drive_service_caps_socket_timeout_at_60_seconds() -> None:
    """§V.49: Drive socket-timeout cap bounds stall window so the retry
    classifier sees ``socket.timeout`` quickly."""
    with (
        patch("mailpilot.gmail.build_delegated_credentials") as mock_creds,
        patch("googleapiclient.discovery.build") as mock_build,
        patch("httplib2.Http") as mock_http,
        patch("google_auth_httplib2.AuthorizedHttp") as mock_authed,
    ):
        mock_creds.return_value = MagicMock()
        mock_http.return_value = MagicMock()
        mock_authed.return_value = MagicMock()

        build_drive_service("user@example.com")

    mock_http.assert_called_once_with(timeout=60)
    mock_authed.assert_called_once_with(
        mock_creds.return_value, http=mock_http.return_value
    )
    build_kwargs = mock_build.call_args.kwargs
    build_args = mock_build.call_args.args
    assert build_args == ("drive", "v3")
    assert build_kwargs == {"http": mock_authed.return_value}
