"""Tests for settings loading and persistence."""

import json
from pathlib import Path
from typing import Any

import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError

from mailpilot.settings import Settings, load_settings, save_settings, set_setting


def test_default_settings():
    settings = Settings()
    assert str(settings.database_url) == "postgresql://localhost/mailpilot"
    assert settings.llm_provider == "xai"
    assert settings.anthropic_model == "claude-sonnet-5"
    assert settings.anthropic_base_url == "https://api.anthropic.com"
    assert settings.xai_model == "grok-4.5"
    assert settings.xai_reasoning_effort == "medium"
    assert settings.xai_max_tokens == 32768
    assert settings.xai_api_host == ""
    assert settings.logfire_environment == "development"
    assert settings.google_pubsub_topic == "mailpilot-topic-dev"


def test_anthropic_reasoning_defaults_active():
    """§V.47: workflow agent reasons by default -- thinking='adaptive', effort='high'."""
    settings = Settings()
    assert settings.anthropic_thinking == "adaptive"
    assert settings.anthropic_effort == "high"


def test_anthropic_reasoning_disables_per_knob():
    """§V.47: an operator opts out per knob by setting it to ''."""
    settings = Settings(anthropic_thinking="", anthropic_effort="")
    assert settings.anthropic_thinking == ""
    assert settings.anthropic_effort == ""


def test_set_setting_round_trips_reasoning_keys(tmp_path: Path):
    """§V.47: config set/get persists the reasoning controls."""
    config_path = tmp_path / "config.json"
    set_setting("anthropic_thinking", "adaptive", config_path=config_path)
    set_setting("anthropic_effort", "high", config_path=config_path)
    reloaded = load_settings(config_path=config_path)
    assert reloaded.anthropic_thinking == "adaptive"
    assert reloaded.anthropic_effort == "high"


def test_anthropic_max_tokens_default():
    """§V.47: anthropic_max_tokens defaults to 32768 (output budget bounded)."""
    settings = Settings()
    assert settings.anthropic_max_tokens == 32768


def test_set_setting_round_trips_max_tokens(tmp_path: Path):
    """§V.47: config set/get persists the output-token budget override."""
    config_path = tmp_path / "config.json"
    set_setting("anthropic_max_tokens", 32768, config_path=config_path)
    reloaded = load_settings(config_path=config_path)
    assert reloaded.anthropic_max_tokens == 32768


def test_anthropic_thinking_rejects_invalid_value():
    """§V.47: anthropic_thinking is a closed Literal; an off-list value is rejected."""
    with pytest.raises(ValidationError):
        Settings(anthropic_thinking="enabled")  # pyright: ignore[reportArgumentType]


def test_anthropic_effort_rejects_invalid_value():
    """§V.47: anthropic_effort is a closed Literal; an off-list value is rejected."""
    with pytest.raises(ValidationError):
        Settings(anthropic_effort="extreme")  # pyright: ignore[reportArgumentType]


def test_xai_reasoning_effort_rejects_invalid_value():
    """§V.47: xai_reasoning_effort is a closed Literal; off-list values rejected."""
    with pytest.raises(ValidationError):
        Settings(xai_reasoning_effort="none")  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValidationError):
        Settings(xai_reasoning_effort="xhigh")  # pyright: ignore[reportArgumentType]


def test_llm_provider_rejects_invalid_value():
    """§V.47: llm_provider is a closed Literal {anthropic, xai}."""
    with pytest.raises(ValidationError):
        Settings(llm_provider="openai")  # pyright: ignore[reportArgumentType]


def test_xai_api_key_env_uses_mailpilot_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§V.47: bare XAI_API_KEY is not a mailpilot source; MAILPILOT_XAI_API_KEY is."""
    monkeypatch.setenv("XAI_API_KEY", "bare-should-not-win")
    monkeypatch.delenv("MAILPILOT_XAI_API_KEY", raising=False)
    settings = Settings()
    assert settings.xai_api_key == ""
    monkeypatch.setenv("MAILPILOT_XAI_API_KEY", "mailpilot-key")
    settings = Settings()
    assert settings.xai_api_key == "mailpilot-key"


def test_set_setting_round_trips_xai_keys(tmp_path: Path):
    """§V.47: config set/get persists xAI knobs."""
    config_path = tmp_path / "config.json"
    set_setting("llm_provider", "xai", config_path=config_path)
    set_setting("xai_model", "grok-4.5", config_path=config_path)
    set_setting("xai_reasoning_effort", "high", config_path=config_path)
    set_setting("xai_max_tokens", 16384, config_path=config_path)
    reloaded = load_settings(config_path=config_path)
    assert reloaded.llm_provider == "xai"
    assert reloaded.xai_model == "grok-4.5"
    assert reloaded.xai_reasoning_effort == "high"
    assert reloaded.xai_max_tokens == 16384


def test_set_setting_redacts_xai_api_key(
    capfire: CaptureLogfire, tmp_path: Path
) -> None:
    """§V.86: xai_api_key is secret; config.set redacts old/new."""
    config_path = tmp_path / "config.json"
    secret = "xai-super-secret-do-not-leak"
    set_setting("xai_api_key", secret, config_path=config_path)
    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert secret not in str(attr_value)


def test_anthropic_base_url_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """MAILPILOT_ANTHROPIC_BASE_URL overrides the None default per §V.85."""
    monkeypatch.setenv(
        "MAILPILOT_ANTHROPIC_BASE_URL", "https://api.novita.ai/anthropic"
    )
    settings = Settings()
    assert settings.anthropic_base_url == "https://api.novita.ai/anthropic"


def test_run_interval_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mailpilot.settings.CONFIG_PATH", tmp_path / "config.json")
    settings = Settings()
    assert settings.run_interval == 60


def test_max_concurrent_tasks_default_meets_burst_formula(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mailpilot.settings.CONFIG_PATH", tmp_path / "config.json")
    settings = Settings()
    assert settings.max_concurrent_tasks >= 10


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


def test_dotenv_overrides_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd ``.env`` beats ``~/.mailpilot/config.json`` per §V.85."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"logfire_environment": "development"}))
    monkeypatch.setattr("mailpilot.settings.CONFIG_PATH", config_path)

    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("MAILPILOT_LOGFIRE_ENVIRONMENT=production\n")
    monkeypatch.chdir(workdir)

    settings = Settings()
    assert settings.logfire_environment == "production"


def test_process_env_beats_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Process ``MAILPILOT_*`` env beats cwd ``.env`` per §V.85."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("MAILPILOT_LOGFIRE_ENVIRONMENT=development\n")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")

    settings = Settings()
    assert settings.logfire_environment == "production"


def test_kwargs_beat_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor kwargs beat cwd ``.env`` per §V.85."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("MAILPILOT_LOGFIRE_ENVIRONMENT=production\n")
    monkeypatch.chdir(workdir)

    settings = Settings(logfire_environment="development")
    assert settings.logfire_environment == "development"


def test_missing_dotenv_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing cwd ``.env`` is a no-op; field defaults still apply per §V.85."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    settings = Settings()
    assert settings.logfire_environment == "development"
    assert settings.run_interval == 60


def test_dotenv_ignores_non_mailpilot_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-``MAILPILOT_*`` keys in ``.env`` are ignored (no crash, no field bleed)."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text(
        "PROD_DB_HOST=db.example.com\nMAILPILOT_RUN_INTERVAL=42\n"
    )
    monkeypatch.chdir(workdir)

    settings = Settings()
    assert settings.run_interval == 42
    assert not hasattr(settings, "prod_db_host")


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
    assert attrs["old"] == "claude-sonnet-5"
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


def test_set_setting_redacts_database_url(capfire: CaptureLogfire, tmp_path: Path):
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
