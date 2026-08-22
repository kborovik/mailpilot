"""Tests for two-phase settings load and ``app_config`` persistence."""

from pathlib import Path
from typing import Any

import psycopg
import pytest
from logfire.testing import CaptureLogfire
from pydantic import ValidationError

from mailpilot.settings import (
    APP_CONFIG_KEYS,
    Settings,
    bootstrap_database_url,
    load_settings,
    require_active_provider_key,
    set_setting,
)


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
    assert settings.environment == "dev"
    assert settings.google_application_credentials is None
    assert "logfire_environment" not in Settings.model_fields
    assert "logfire_environment" not in Settings.model_computed_fields
    assert not hasattr(settings, "logfire_environment")
    assert settings.google_pubsub_topic == "mailpilot-topic-dev"
    assert settings.google_pubsub_subscription == "mailpilot-sub-dev"


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


def test_set_setting_round_trips_reasoning_keys(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: config set/get persists the reasoning controls."""
    set_setting(database_connection, "anthropic_thinking", "adaptive")
    set_setting(database_connection, "anthropic_effort", "high")
    reloaded = load_settings(connection=database_connection)
    assert reloaded.anthropic_thinking == "adaptive"
    assert reloaded.anthropic_effort == "high"


def test_anthropic_max_tokens_default():
    """§V.47: anthropic_max_tokens defaults to 32768 (output budget bounded)."""
    settings = Settings()
    assert settings.anthropic_max_tokens == 32768


def test_set_setting_round_trips_max_tokens(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: config set/get persists the output-token budget override."""
    set_setting(database_connection, "anthropic_max_tokens", 32768)
    reloaded = load_settings(connection=database_connection)
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


def test_xai_api_key_env_is_not_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§V.85: bare XAI_API_KEY and MAILPILOT_XAI_API_KEY are not sources."""
    monkeypatch.setenv("XAI_API_KEY", "bare-should-not-win")
    monkeypatch.setenv("MAILPILOT_XAI_API_KEY", "mailpilot-key")
    settings = Settings()
    assert settings.xai_api_key == ""


def test_set_setting_round_trips_xai_keys(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.47: config set/get persists xAI knobs."""
    set_setting(database_connection, "llm_provider", "xai")
    set_setting(database_connection, "xai_model", "grok-4.5")
    set_setting(database_connection, "xai_reasoning_effort", "high")
    set_setting(database_connection, "xai_max_tokens", 16384)
    reloaded = load_settings(connection=database_connection)
    assert reloaded.llm_provider == "xai"
    assert reloaded.xai_model == "grok-4.5"
    assert reloaded.xai_reasoning_effort == "high"
    assert reloaded.xai_max_tokens == 16384


def test_set_setting_redacts_xai_api_key(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.86: xai_api_key is secret; config.set redacts old/new."""
    secret = "xai-super-secret-do-not-leak"
    set_setting(database_connection, "xai_api_key", secret)
    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert secret not in str(attr_value)


def test_set_setting_redacts_google_credentials(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.86: google_application_credentials is secret."""
    secret = {"type": "service_account", "private_key": "leak-me-not"}
    set_setting(database_connection, "google_application_credentials", secret)
    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert "leak-me-not" not in str(attr_value)


def test_anthropic_base_url_env_is_not_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§V.85: MAILPILOT_ANTHROPIC_BASE_URL is not a mailpilot source."""
    monkeypatch.setenv(
        "MAILPILOT_ANTHROPIC_BASE_URL", "https://api.novita.ai/anthropic"
    )
    settings = Settings()
    assert settings.anthropic_base_url == "https://api.anthropic.com"


def test_run_interval_default() -> None:
    settings = Settings()
    assert settings.run_interval == 60


def test_max_concurrent_tasks_default_meets_burst_formula() -> None:
    settings = Settings()
    assert settings.max_concurrent_tasks >= 10


def test_settings_from_kwargs():
    settings = Settings(environment="prd", anthropic_api_key="sk-test")
    assert settings.environment == "prd"
    assert settings.anthropic_api_key == "sk-test"


def test_settings_env_does_not_override_app_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§V.85: MAILPILOT_ENVIRONMENT is not a source."""
    monkeypatch.setenv("MAILPILOT_ENVIRONMENT", "prd")
    settings = Settings()
    assert settings.environment == "dev"


def test_bootstrap_url_kwargs_beat_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAILPILOT_DATABASE_URL", "postgresql://env/db")
    assert (
        bootstrap_database_url(database_url="postgresql://kw/db")
        == "postgresql://kw/db"
    )


def test_bootstrap_url_env_beats_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("MAILPILOT_DATABASE_URL=postgresql://dotenv/db\n")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("MAILPILOT_DATABASE_URL", "postgresql://env/db")
    assert bootstrap_database_url() == "postgresql://env/db"


def test_bootstrap_url_dotenv_beats_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text(
        "MAILPILOT_DATABASE_URL=postgresql://dotenv/db\nMAILPILOT_ENVIRONMENT=prd\n"
    )
    monkeypatch.chdir(workdir)
    monkeypatch.delenv("MAILPILOT_DATABASE_URL", raising=False)
    assert bootstrap_database_url() == "postgresql://dotenv/db"


def test_bootstrap_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAILPILOT_DATABASE_URL", raising=False)
    monkeypatch.chdir("/tmp")
    assert bootstrap_database_url() == "postgresql://localhost/mailpilot"


def test_dotenv_does_not_set_app_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd ``.env`` MAILPILOT_* other than DATABASE_URL do not hydrate Settings."""
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text(
        "MAILPILOT_ENVIRONMENT=prd\nMAILPILOT_RUN_INTERVAL=42\n"
    )
    monkeypatch.chdir(workdir)
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.run_interval == 60


def test_missing_dotenv_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.delenv("MAILPILOT_DATABASE_URL", raising=False)
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.run_interval == 60


def test_load_settings_inserts_missing_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.181: missing singleton @ load inserts column defaults."""
    database_connection.execute("DELETE FROM app_config")
    database_connection.commit()
    settings = load_settings(connection=database_connection)
    assert settings.environment == "dev"
    assert settings.llm_provider == "xai"
    row = database_connection.execute(
        "SELECT id, environment FROM app_config WHERE id = 'singleton'"
    ).fetchone()
    assert row is not None
    assert row["environment"] == "dev"


def test_load_settings_hydrates_from_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    set_setting(database_connection, "environment", "prd")
    set_setting(database_connection, "anthropic_api_key", "sk-123")
    loaded = load_settings(connection=database_connection)
    assert loaded.environment == "prd"
    assert loaded.anthropic_api_key == "sk-123"
    assert loaded.google_pubsub_topic == "mailpilot-topic-prd"


def test_load_settings_kwargs_override_row(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.85: kwargs (tests) beat the app_config row."""
    set_setting(database_connection, "environment", "prd")
    loaded = load_settings(connection=database_connection, environment="dev")
    assert loaded.environment == "dev"


def test_set_setting_rejects_unknown_key(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    with pytest.raises(KeyError):
        set_setting(database_connection, "not_a_real_field", "x")


def test_set_setting_rejects_database_url(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.181: config set database_url is invalid_key."""
    with pytest.raises(KeyError):
        set_setting(
            database_connection,
            "database_url",
            "postgresql://other/db",
        )


def test_set_setting_preserves_other_fields(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    set_setting(database_connection, "anthropic_api_key", "sk-keep")
    set_setting(database_connection, "environment", "prd")
    set_setting(database_connection, "anthropic_model", "claude-opus-4-7")
    reloaded = load_settings(connection=database_connection)
    assert reloaded.anthropic_api_key == "sk-keep"
    assert reloaded.environment == "prd"
    assert reloaded.anthropic_model == "claude-opus-4-7"


def _config_set_logs(capfire: CaptureLogfire) -> list[dict[str, Any]]:
    return [
        span
        for span in capfire.exporter.exported_spans_as_dict()
        if span["name"] == "config.set"
    ]


def test_set_setting_emits_telemetry_with_value_for_non_secret(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """config.set logs old/new values for non-secret keys."""
    new_model = "claude-opus-4-7"
    set_setting(database_connection, "anthropic_model", new_model)

    logs = _config_set_logs(capfire)
    assert len(logs) == 1
    attrs = logs[0]["attributes"]
    assert attrs["key"] == "anthropic_model"
    assert attrs["changed"] is True
    assert attrs["old"] == "claude-sonnet-5"
    assert attrs["new"] == new_model


def test_set_setting_does_not_leak_secret_values(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    """Setting a secret key must redact both old and new values."""
    secret = "sk-super-secret-do-not-leak"
    set_setting(database_connection, "anthropic_api_key", secret)

    for span in capfire.exporter.exported_spans_as_dict():
        for attr_value in span.get("attributes", {}).values():
            assert secret not in str(attr_value)


def test_set_setting_changed_false_when_value_unchanged(
    capfire: CaptureLogfire,
    database_connection: psycopg.Connection[dict[str, Any]],
):
    set_setting(database_connection, "anthropic_model", "claude-opus-4-7")
    capfire.exporter.clear()
    set_setting(database_connection, "anthropic_model", "claude-opus-4-7")

    logs = _config_set_logs(capfire)
    assert len(logs) == 1
    assert logs[0]["attributes"]["changed"] is False


def test_prd_derives_topic_and_sub() -> None:
    """§V.176: environment=prd derives topic/sub."""
    settings = Settings(environment="prd")
    assert settings.google_pubsub_topic == "mailpilot-topic-prd"
    assert settings.google_pubsub_subscription == "mailpilot-sub-prd"


def test_environment_rejects_invalid_value() -> None:
    """§V.176: environment is a closed Literal {dev, prd}."""
    with pytest.raises(ValidationError):
        Settings(environment="staging")  # pyright: ignore[reportArgumentType]


def test_derived_env_vars_are_not_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    """§V.176: MAILPILOT_LOGFIRE_ENVIRONMENT / topic / sub are not sources."""
    monkeypatch.setenv("MAILPILOT_LOGFIRE_ENVIRONMENT", "production")
    monkeypatch.setenv("MAILPILOT_GOOGLE_PUBSUB_TOPIC", "custom-topic")
    monkeypatch.setenv("MAILPILOT_GOOGLE_PUBSUB_SUBSCRIPTION", "custom-sub")
    settings = Settings()
    assert settings.environment == "dev"
    assert settings.google_pubsub_topic == "mailpilot-topic-dev"
    assert settings.google_pubsub_subscription == "mailpilot-sub-dev"


def test_set_setting_rejects_derived_keys(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.176: config set of derived keys is invalid_key (KeyError)."""
    for key in (
        "logfire_environment",
        "google_pubsub_topic",
        "google_pubsub_subscription",
    ):
        with pytest.raises(KeyError):
            set_setting(database_connection, key, "x")


def test_set_setting_environment_round_trip(
    database_connection: psycopg.Connection[dict[str, Any]],
) -> None:
    """§V.176: config set environment persists and derives names."""
    updated = set_setting(database_connection, "environment", "prd")
    assert updated.environment == "prd"
    reloaded = load_settings(connection=database_connection)
    assert reloaded.environment == "prd"
    row = database_connection.execute(
        "SELECT environment FROM app_config WHERE id = 'singleton'"
    ).fetchone()
    assert row is not None
    assert row["environment"] == "prd"


def test_app_config_keys_match_settings_minus_url_and_derived() -> None:
    persistable = set(Settings.model_fields) - {"database_url"}
    assert set(APP_CONFIG_KEYS) == persistable


def test_no_config_json_symbols() -> None:
    """§V.85: settings module has no config.json path."""
    import inspect

    import mailpilot.settings as settings_mod

    source = inspect.getsource(settings_mod)
    assert "config.json" not in source
    assert "CONFIG_PATH" not in source


def test_require_active_provider_key_names_config_set() -> None:
    """§V.47: missing key names ``mailpilot config set``, not MAILPILOT_*."""
    with pytest.raises(ValueError, match="mailpilot config set xai_api_key") as exc:
        require_active_provider_key(Settings(llm_provider="xai", xai_api_key=""))
    assert "MAILPILOT_XAI_API_KEY" not in str(exc.value)
    with pytest.raises(
        ValueError, match="mailpilot config set anthropic_api_key"
    ) as exc_a:
        require_active_provider_key(
            Settings(llm_provider="anthropic", anthropic_api_key="")
        )
    assert "MAILPILOT_ANTHROPIC_API_KEY" not in str(exc_a.value)
