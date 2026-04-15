"""Tests for settings loading and persistence."""

import json
from pathlib import Path

import pytest

from mailpilot.settings import Settings, load_settings, save_settings


def test_default_settings():
    settings = Settings()
    assert str(settings.database_url) == "postgresql://localhost/mailpilot"
    assert settings.anthropic_model == "claude-sonnet-4-6"
    assert settings.logfire_environment == "development"
    assert settings.google_pubsub_topic == "gmail-watch"


def test_settings_from_kwargs():
    settings = Settings(logfire_environment="staging", anthropic_api_key="sk-test")
    assert settings.logfire_environment == "staging"
    assert settings.anthropic_api_key == "sk-test"


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")
    settings = Settings()
    assert settings.logfire_environment == "production"


def test_settings_kwargs_override_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")
    settings = Settings(logfire_environment="test")
    assert settings.logfire_environment == "test"


def test_save_and_load_settings(tmp_path: Path):
    config_path = tmp_path / "config.json"
    original = Settings(logfire_environment="staging", anthropic_api_key="sk-123")
    save_settings(original, config_path=config_path)

    loaded = load_settings(config_path=config_path)
    assert loaded.logfire_environment == "staging"
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
        json.dumps({"unknown_key": "value", "logfire_environment": "custom"})
    )
    settings = load_settings(config_path=config_path)
    assert settings.logfire_environment == "custom"
    assert not hasattr(settings, "unknown_key")
