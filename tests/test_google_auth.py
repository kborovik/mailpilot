"""Tests for shared Google credential helpers and the service factory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mailpilot import google_auth
from mailpilot.settings import Settings

_FAKE_SA = {
    "type": "service_account",
    "project_id": "test-proj",
    "client_email": "sa@test-proj.iam.gserviceaccount.com",
}


def test_google_sa_info_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(google_application_credentials=_FAKE_SA)
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert google_auth._google_sa_info() == _FAKE_SA  # pyright: ignore[reportPrivateUsage]


def test_google_sa_info_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(google_application_credentials=None)
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert google_auth._google_sa_info() is None  # pyright: ignore[reportPrivateUsage]


def test_has_google_credentials_true_when_json_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(google_application_credentials=_FAKE_SA)
    with patch("mailpilot.settings.get_settings", return_value=settings):
        assert google_auth.has_google_credentials() is True


def test_has_google_credentials_true_when_adc_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(google_application_credentials=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch("google.auth.default", return_value=(object(), "proj")),
    ):
        assert google_auth.has_google_credentials() is True


def test_has_google_credentials_false_when_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.auth.exceptions import DefaultCredentialsError

    settings = Settings(google_application_credentials=None)
    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch("google.auth.default", side_effect=DefaultCredentialsError()),
    ):
        assert google_auth.has_google_credentials() is False


def test_build_delegated_credentials_uses_json_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSONB document → from_service_account_info + with_subject (no ADC)."""
    settings = Settings(google_application_credentials=_FAKE_SA)

    json_creds = type("C", (), {"with_subject": lambda self, s: ("sub", s)})()

    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch(
            "google.oauth2.service_account.Credentials.from_service_account_info",
            return_value=json_creds,
        ) as mock_from_info,
    ):
        result = google_auth.build_delegated_credentials(["scope1"], "user@example.com")

    mock_from_info.assert_called_once_with(_FAKE_SA, scopes=["scope1"])
    assert result == ("sub", "user@example.com")


def test_build_delegated_credentials_falls_back_to_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Null JSONB → ADC + iam.Signer + service_account.Credentials(subject=...)."""
    settings = Settings(google_application_credentials=None)

    source_creds = type(
        "Source",
        (),
        {"service_account_email": "sa@proj.iam.gserviceaccount.com"},
    )()
    sentinel_signer = object()
    sentinel_credentials = object()

    with (
        patch("mailpilot.settings.get_settings", return_value=settings),
        patch(
            "google.auth.default", return_value=(source_creds, "proj")
        ) as mock_default,
        patch("google.auth.iam.Signer", return_value=sentinel_signer) as mock_signer,
        patch(
            "google.oauth2.service_account.Credentials",
            return_value=sentinel_credentials,
        ) as mock_credentials_cls,
    ):
        result = google_auth.build_delegated_credentials(["scope1"], "user@example.com")

    mock_default.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    mock_signer.assert_called_once()
    mock_credentials_cls.assert_called_once()
    kwargs = mock_credentials_cls.call_args.kwargs
    assert kwargs["service_account_email"] == "sa@proj.iam.gserviceaccount.com"
    assert kwargs["subject"] == "user@example.com"
    assert kwargs["scopes"] == ["scope1"]
    assert result is sentinel_credentials


def test_build_delegated_service_passes_credentials_when_http_omitted() -> None:
    sentinel_creds = object()
    sentinel_service = object()
    with (
        patch(
            "mailpilot.google_auth.build_delegated_credentials",
            return_value=sentinel_creds,
        ) as mock_creds,
        patch(
            "googleapiclient.discovery.build", return_value=sentinel_service
        ) as mock_build,
    ):
        result = google_auth.build_delegated_service(
            "gmail", "v1", ["scope1"], "user@example.com"
        )

    mock_creds.assert_called_once_with(["scope1"], "user@example.com")
    mock_build.assert_called_once_with("gmail", "v1", credentials=sentinel_creds)
    assert result is sentinel_service


def test_build_delegated_service_wraps_custom_http() -> None:
    sentinel_creds = object()
    sentinel_http = object()
    sentinel_authed = object()
    sentinel_service = object()
    with (
        patch(
            "mailpilot.google_auth.build_delegated_credentials",
            return_value=sentinel_creds,
        ),
        patch(
            "google_auth_httplib2.AuthorizedHttp", return_value=sentinel_authed
        ) as mock_authed,
        patch(
            "googleapiclient.discovery.build", return_value=sentinel_service
        ) as mock_build,
    ):
        result = google_auth.build_delegated_service(
            "drive", "v3", ["scope1"], "user@example.com", http=sentinel_http
        )

    mock_authed.assert_called_once_with(sentinel_creds, http=sentinel_http)
    mock_build.assert_called_once_with("drive", "v3", http=sentinel_authed)
    assert result is sentinel_service


def test_google_client_from_service_skips_factory() -> None:
    from typing import ClassVar

    service = MagicMock()

    class _Stub(google_auth.GoogleClient):
        _api = "gmail"
        _version = "v1"
        _scopes: ClassVar[list[str]] = ["scope1"]

    client = _Stub.from_service("user@example.com", service)
    assert client.email == "user@example.com"
    assert client._service is service  # pyright: ignore[reportPrivateUsage]


def test_google_transient_statuses_exclude_529_and_are_shared() -> None:
    """§V.189 / §V.49: one Google set, no 529; Anthropic 529 stays Anthropic."""
    from mailpilot.agent import retry
    from mailpilot.google_auth import GOOGLE_TRANSIENT_STATUSES

    assert 529 not in GOOGLE_TRANSIENT_STATUSES
    assert frozenset({429, 500, 502, 503, 504}) == GOOGLE_TRANSIENT_STATUSES
    assert retry.GOOGLE_TRANSIENT_STATUSES is GOOGLE_TRANSIENT_STATUSES


def test_drive_and_calendar_import_google_auth_not_gmail() -> None:
    import ast
    from pathlib import Path

    def _imported_modules(path: str) -> set[str]:
        tree = ast.parse(Path(path).read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
        return names

    drive_imports = _imported_modules("src/mailpilot/drive.py")
    calendar_imports = _imported_modules("src/mailpilot/calendar.py")
    pubsub_imports = _imported_modules("src/mailpilot/pubsub.py")
    assert "mailpilot.gmail" not in drive_imports
    assert "mailpilot.gmail" not in calendar_imports
    assert "mailpilot.google_auth" in drive_imports
    assert "mailpilot.google_auth" in calendar_imports
    assert "mailpilot.google_auth" in pubsub_imports
    assert "mailpilot.gmail" in pubsub_imports  # GmailClient for watch renewal
