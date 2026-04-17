"""Tests for settings loading and persistence."""

import json
from pathlib import Path

import pytest

from mailpilot.settings import Settings, load_settings, save_settings, set_setting


def test_default_settings():
    settings = Settings()
    assert str(settings.database_url) == "postgresql://localhost/mailpilot"
    assert settings.anthropic_model == "claude-sonnet-4-6"
    assert settings.logfire_environment == "development"
    assert settings.google_pubsub_topic == "gmail-watch"


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
