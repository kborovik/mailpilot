"""Application settings: two-phase load from env URL + ``app_config``.

Phase 1 — bootstrap ``database_url`` (process-lifetime):
    kwargs > env ``MAILPILOT_DATABASE_URL`` > cwd ``.env`` > default
    ``postgresql://localhost/mailpilot``.

Phase 2 — hydrate the rest from the ``app_config`` singleton row:
    kwargs (tests) > row > field-literal defaults.

``MAILPILOT_*`` env vars other than ``MAILPILOT_DATABASE_URL`` are not
sources. There is no JSON config file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import logfire
from pydantic import BaseModel, PostgresDsn, computed_field

TargetEnvironment = Literal["dev", "prd"]

# Global LLM provider switch (§V.47). Default is xAI; Anthropic is opt-in.
LlmProvider = Literal["anthropic", "xai"]

# Workflow-agent reasoning controls (§V.47). '' turns a knob off (no key sent);
# the defaults below enable both. These gate extended thinking and reasoning
# effort on the workflow agent only -- the classifier never reads them.
AnthropicThinking = Literal["", "adaptive"]
AnthropicEffort = Literal["", "low", "medium", "high", "xhigh", "max"]
# xAI reasoning effort: closed set, no empty/none -- Grok 4.5 always reasons.
XaiReasoningEffort = Literal["low", "medium", "high"]

DEFAULT_DATABASE_URL = "postgresql://localhost/mailpilot"

# Fields persisted on ``app_config`` (every Settings field except
# ``database_url`` and derived pubsub names).
APP_CONFIG_KEYS = (
    "logfire_token",
    "environment",
    "llm_provider",
    "anthropic_api_key",
    "anthropic_model",
    "anthropic_base_url",
    "anthropic_thinking",
    "anthropic_effort",
    "anthropic_max_tokens",
    "xai_api_key",
    "xai_model",
    "xai_api_host",
    "xai_reasoning_effort",
    "xai_max_tokens",
    "google_application_credentials",
    "run_interval",
    "max_concurrent_tasks",
)

DERIVED_KEYS = frozenset({"google_pubsub_topic", "google_pubsub_subscription"})

# Fields whose values must never appear in telemetry. database_url can carry
# user:password@host credentials, so it is treated as secret too.
SECRET_KEYS = frozenset(
    {
        "anthropic_api_key",
        "xai_api_key",
        "logfire_token",
        "database_url",
        "google_application_credentials",
    }
)
REDACTED = "***"


class Settings(BaseModel):
    """MailPilot configuration."""

    database_url: PostgresDsn = PostgresDsn(DEFAULT_DATABASE_URL)
    logfire_token: str = ""
    environment: TargetEnvironment = "dev"
    llm_provider: LlmProvider = "xai"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_thinking: AnthropicThinking = "adaptive"
    anthropic_effort: AnthropicEffort = "high"
    anthropic_max_tokens: int = 32768
    xai_api_key: str = ""
    xai_model: str = "grok-4.5"
    xai_api_host: str = ""
    xai_reasoning_effort: XaiReasoningEffort = "medium"
    xai_max_tokens: int = 32768
    google_application_credentials: dict[str, Any] | None = None
    run_interval: int = 60
    max_concurrent_tasks: int = 10

    @computed_field
    @property
    def google_pubsub_topic(self) -> str:
        """Pub/Sub topic derived from ``environment`` (§V.176)."""
        return f"mailpilot-topic-{self.environment}"

    @computed_field
    @property
    def google_pubsub_subscription(self) -> str:
        """Pub/Sub subscription derived from ``environment`` (§V.176)."""
        return f"mailpilot-sub-{self.environment}"


_CACHE: dict[str, Settings] = {}


def _read_dotenv_database_url() -> str | None:
    """Return ``MAILPILOT_DATABASE_URL`` from cwd ``.env``, or None."""
    path = Path(".env")
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if not line.startswith("MAILPILOT_DATABASE_URL="):
            continue
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value
    return None


def bootstrap_database_url(*, database_url: str | None = None) -> str:
    """Resolve the process-lifetime database URL (§V.1 / §V.85).

    Order: kwargs > env ``MAILPILOT_DATABASE_URL`` > cwd ``.env`` >
    ``postgresql://localhost/mailpilot``.
    """
    import os

    if database_url:
        return database_url
    env = os.environ.get("MAILPILOT_DATABASE_URL")
    if env:
        return env
    dotenv = _read_dotenv_database_url()
    if dotenv:
        return dotenv
    return DEFAULT_DATABASE_URL


def settings_from_app_config_row(
    row: dict[str, Any], *, database_url: str, **overrides: Any
) -> Settings:
    """Build ``Settings`` from an ``app_config`` row plus bootstrap URL."""
    data: dict[str, Any] = {key: row[key] for key in APP_CONFIG_KEYS if key in row}
    data.update(overrides)
    return Settings(database_url=database_url, **data)  # pyright: ignore[reportArgumentType]


def cache_settings(settings: Settings) -> Settings:
    """Store ``settings`` as the process cache and return it."""
    _CACHE["current"] = settings
    return settings


def clear_settings_cache() -> None:
    """Drop the process settings cache (tests)."""
    _CACHE.clear()


def load_settings(
    connection: Any | None = None,
    **overrides: Any,
) -> Settings:
    """Two-phase load: bootstrap URL, then hydrate from ``app_config``.

    Missing singleton row is inserted with column defaults. ``connection``
    if given is used as-is (not closed). ``database_url`` in ``overrides``
    is bootstrap-only and never written to the row.
    """
    from mailpilot.database import get_or_insert_app_config, initialize_database

    url = bootstrap_database_url(
        database_url=overrides.pop("database_url", None),
    )
    own_connection = connection is None
    if connection is None:
        connection = initialize_database(url)
    try:
        row = get_or_insert_app_config(connection)
        settings = settings_from_app_config_row(row, database_url=url, **overrides)
        return cache_settings(settings)
    finally:
        if own_connection:
            connection.close()


def get_settings() -> Settings:
    """Return cached settings, loading from ``app_config`` on first call."""
    cached = _CACHE.get("current")
    if cached is None:
        return load_settings()
    return cached


def require_active_provider_key(settings: Settings) -> None:
    """Raise if the selected LLM provider's API key is missing or empty.

    §V.47 / §I.config: each ``mailpilot run`` tick calls this before drain
    so a missing key never claims due tasks. ``build_model`` calls it so
    model construction stays fail-closed if preflight is skipped. The error
    names ``mailpilot config set``.

    Args:
        settings: Loaded application settings.

    Raises:
        ValueError: Active-provider key is missing or empty.
    """
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError(
                "anthropic_api_key is required when llm_provider=anthropic; "
                "set it via `mailpilot config set anthropic_api_key`"
            )
        return
    if not settings.xai_api_key:
        raise ValueError(
            "xai_api_key is required when llm_provider=xai; "
            "set it via `mailpilot config set xai_api_key`"
        )


def set_setting(connection: Any, key: str, value: object) -> Settings:
    """Update one ``app_config`` key, persist, and emit telemetry.

    ``database_url`` and derived pubsub keys raise ``KeyError`` (CLI maps
    that to ``invalid_key``). Invalid field values raise pydantic
    ``ValidationError``.

    Args:
        connection: Open database connection (app_config row).
        key: Persistable Settings field name.
        value: Parsed value to store.

    Returns:
        The updated ``Settings`` instance.

    Raises:
        KeyError: Unknown, derived, or bootstrap-only key.
    """
    from mailpilot.database import get_or_insert_app_config, update_app_config_key

    if key == "database_url" or key in DERIVED_KEYS or key not in APP_CONFIG_KEYS:
        raise KeyError(key)
    current = settings_from_app_config_row(
        get_or_insert_app_config(connection),
        database_url=bootstrap_database_url(),
    )
    data = current.model_dump(mode="python")
    old_value = data.get(key)
    data[key] = value
    persistable = {k: data[k] for k in APP_CONFIG_KEYS}
    updated = Settings(
        database_url=current.database_url,
        **persistable,
    )
    update_app_config_key(connection, key, getattr(updated, key))
    new_value = updated.model_dump(mode="python").get(key)
    is_secret = key in SECRET_KEYS
    logfire.info(
        "config.set",
        key=key,
        changed=old_value != new_value,
        old=REDACTED if is_secret else old_value,
        new=REDACTED if is_secret else new_value,
    )
    return cache_settings(updated)
