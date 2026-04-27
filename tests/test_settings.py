"""Tests for settings loading and persistence."""

import json
from pathlib import Path
from typing import Any

import pytest
from logfire.testing import CaptureLogfire

from mailpilot.settings import Settings, load_settings, save_settings, set_setting


def test_default_settings():
    settings = Settings()
    assert str(settings.database_url) == "postgresql://localhost/mailpilot"
    assert settings.anthropic_model == "claude-sonnet-4-6"
    assert settings.logfire_environment == "development"
    assert settings.google_pubsub_topic == "gmail-watch"


def test_run_interval_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailpilot.settings.CONFIG_PATH", tmp_path / "config.json")
    settings = Settings()
    assert settings.run_interval == 30


def test_settings_from_kwargs():
    settings = Settings(logfire_environment="production", anthropic_api_key="sk-test")
    assert settings.logfire_environment == "production"
    assert settings.anthropic_api_key == "sk-test"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")
    settings = Settings()
    assert settings.logfire_environment == "production"


def test_settings_kwargs_override_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")
    settings = Settings(logfire_environment="development")
    assert settings.logfire_environment == "development"


def test_save_and_load_settings(tmp_path: Path):
    config_path = tmp_path / "config.json"
    original = Settings(logfire_environment="production", anthropic_api_key="sk-123")
    save_settings(original, config_path=config_path)

    loaded = load_settings(config_path=config_path)
    assert loaded.logfire_environment == "production"
    assert loaded.anthropic_api_key == "sk-123"


def test_load_settings_creates_default_file(tmp_path: Path):
    config_path = tmp_path / "subdir" / "config.json"
    settings = load_settings(config_path=config_path)
    assert config_path.exists()
    assert settings.logfire_environment == "development"
    data = json.loads(config_path.read_text())
    assert "database_url" in data


def test_load_settings_ignores_unknown_keys(tmp_path: Path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"unknown_key": "value", "logfire_environment": "production"})
    )
    settings = load_settings(config_path=config_path)
    assert settings.logfire_environment == "production"
    assert not hasattr(settings, "unknown_key")


def test_set_setting_persists_value(tmp_path: Path):
    config_path = tmp_path / "config.json"
    updated = set_setting("anthropic_api_key", "sk-new", config_path=config_path)
    assert updated.anthropic_api_key == "sk-new"
    reloaded = load_settings(config_path=config_path)
    assert reloaded.anthropic_api_key == "sk-new"


def test_set_setting_rejects_unknown_key(tmp_path: Path):
    config_path = tmp_path / "config.json"
    with pytest.raises(KeyError):
        set_setting("not_a_real_field", "x", config_path=config_path)


def test_set_setting_preserves_other_fields(tmp_path: Path):
    config_path = tmp_path / "config.json"
    save_settings(
        Settings(anthropic_api_key="sk-keep", logfire_environment="production"),
        config_path=config_path,
    )
    set_setting("anthropic_model", "claude-opus-4-7", config_path=config_path)
    reloaded = load_settings(config_path=config_path)
    assert reloaded.anthropic_api_key == "sk-keep"
    assert reloaded.logfire_environment == "production"
    assert reloaded.anthropic_model == "claude-opus-4-7"


def _config_set_logs(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "config.set"
    ]


def test_set_setting_emits_telemetry_with_value_for_non_secret(
    capfire: CaptureLogfire, tmp_path: Path
):
    """config.set logs old/new values for non-secret keys."""
    config_path = tmp_path / "config.json"
    new_model = "claude-opus-4-7"
    set_setting("anthropic_model", new_model, config_path=config_path)

    logs = _config_set_logs(capfire)
    assert len(logs) == 1
    attrs = logs[0]["attributes"]
    assert attrs["key"] == "anthropic_model"
    assert attrs["changed"] is True
    assert attrs["old"] == "claude-sonnet-4-6"
    assert attrs["new"] == new_model


def test_set_setting_does_not_leak_secret_values(
    capfire: CaptureLogfire, tmp_path: Path
):
    """Setting a secret key must redact both old and new values."""
    config_path = tmp_path / "config.json"
    secret = "sk-super-secret-do-not-leak"
    set_setting("anthropic_api_key", secret, config_path=config_path)

    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert secret not in str(attr_value)


def test_set_setting_redacts_database_url(
    capfire: CaptureLogfire, tmp_path: Path
):
    """database_url can carry credentials so it must be redacted."""
    config_path = tmp_path / "config.json"
    url_with_creds = "postgresql://user:hunter2@db.example.com/mailpilot"
    set_setting("database_url", url_with_creds, config_path=config_path)

    logs = _config_set_logs(capfire)
    assert len(logs) == 1
    attrs = logs[0]["attributes"]
    assert attrs["old"] == "***"
    assert attrs["new"] == "***"
    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert "hunter2" not in str(attr_value)


def test_set_setting_changed_false_when_value_unchanged(
    capfire: CaptureLogfire, tmp_path: Path
):
    config_path = tmp_path / "config.json"
    set_setting("anthropic_model", "claude-opus-4-7", config_path=config_path)
    capfire.exporter.clear()
    set_setting("anthropic_model", "claude-opus-4-7", config_path=config_path)

    logs = _config_set_logs(capfire)
    assert len(logs) == 1
    assert logs[0]["attributes"]["changed"] is False
