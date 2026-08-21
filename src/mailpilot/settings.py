"""Application settings with env var overrides and JSON file persistence.

Priority (highest to lowest):
1. Constructor kwargs (for tests)
2. Process ``MAILPILOT_*`` environment variables
3. Cwd ``.env`` (``MAILPILOT_*`` keys only, pydantic-settings dotenv)
4. ``~/.mailpilot/config.json`` file
5. Field defaults
"""

import json
from pathlib import Path
from typing import Any, Literal, Protocol

import logfire
from pydantic import PostgresDsn, computed_field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

MAILPILOT_DIR = Path.home() / ".mailpilot"
CONFIG_PATH = MAILPILOT_DIR / "config.json"

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

# Fields whose values must never appear in telemetry. database_url can carry
# user:password@host credentials, so it is treated as secret too.
SECRET_KEYS = frozenset(
    {"anthropic_api_key", "xai_api_key", "logfire_token", "database_url"}
)
REDACTED = "***"


class _SettingsSource(Protocol):
    """Duck-typed pydantic-settings source: callable returning field values."""

    def __call__(self) -> dict[str, Any]: ...


class JsonConfigSource:
    """Load settings from ~/.mailpilot/config.json."""

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        self.settings_cls = settings_cls

    def __call__(self) -> dict[str, Any]:
        """Read config file and return known fields."""
        if not CONFIG_PATH.exists():
            return {}
        data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
        return _compat_config_payload(data, set(self.settings_cls.model_fields))


class Settings(BaseSettings):
    """MailPilot configuration."""

    model_config = SettingsConfigDict(
        env_prefix="MAILPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        # only_existing: known Settings fields only (MAILPILOT_* lookup). Default
        # dotenv injects every .env key as an extra and fails extra=forbid.
        dotenv_filtering="only_existing",
    )

    database_url: PostgresDsn = PostgresDsn("postgresql://localhost/mailpilot")
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
    google_application_credentials: str = ""
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

    @classmethod
    def settings_customise_sources(  # pyright: ignore[reportIncompatibleMethodOverride]
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[_SettingsSource, ...]:
        """Set source priority: kwargs > process env > cwd .env > config file."""
        del file_secret_settings  # unused; secrets stay in env / config / dotenv
        json_source: _SettingsSource = JsonConfigSource(settings_cls)
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            json_source,
        )


def _compat_config_payload(data: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    """Keep known persistable keys; map legacy logfire_environment (§V.176)."""
    known = {k: v for k, v in data.items() if k in fields}
    if "environment" not in data:
        if data.get("logfire_environment") == "production":
            known["environment"] = "prd"
        elif "logfire_environment" in data:
            known["environment"] = "dev"
    return known


def persistable_settings_dump(settings: Settings) -> dict[str, Any]:
    """JSON dict for config.json. Omits derived keys (§V.176)."""
    return settings.model_dump(
        mode="json", exclude=set(type(settings).model_computed_fields)
    )


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    """Load settings from all sources.

    Creates the config file with defaults on first run.

    Args:
        config_path: Path to the config file. Defaults to ~/.mailpilot/config.json.

    Returns:
        Settings with values merged from env vars and config file.
    """
    if not config_path.exists():
        defaults = Settings()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(persistable_settings_dump(defaults), indent=2) + "\n"
        )
        return defaults

    if config_path == CONFIG_PATH:
        return Settings()

    # Non-default path: read file directly and pass as kwargs so
    # JsonConfigSource (which hardcodes CONFIG_PATH) is bypassed.
    data: dict[str, Any] = json.loads(config_path.read_text())
    overrides = _compat_config_payload(data, set(Settings.model_fields))
    return Settings(**overrides)


def save_settings(settings: Settings, config_path: Path = CONFIG_PATH) -> None:
    """Save settings to a JSON config file.

    Args:
        settings: Settings to save.
        config_path: Path to the config file. Defaults to ~/.mailpilot/config.json.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(persistable_settings_dump(settings), indent=2)
    config_path.write_text(data + "\n")


def get_settings() -> Settings:
    """Load settings from the default config path."""
    return load_settings()


def require_active_provider_key(settings: Settings) -> None:
    """Raise if the selected LLM provider's API key is missing or empty.

    §V.47 / §I.config: ``mailpilot run`` calls this before drain so a
    missing key never claims or fails due tasks. ``_build_model`` calls
    it so model construction stays fail-closed if preflight is skipped.
    The error names the env var; keys also load from cwd ``.env`` and
    process env per §V.85.

    Args:
        settings: Loaded application settings.

    Raises:
        ValueError: Active-provider key is missing or empty.
    """
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError(
                "MAILPILOT_ANTHROPIC_API_KEY is required when "
                "llm_provider=anthropic; set the environment variable "
                "or a cwd .env file"
            )
        return
    if not settings.xai_api_key:
        raise ValueError(
            "MAILPILOT_XAI_API_KEY is required when llm_provider=xai; "
            "set the environment variable or a cwd .env file"
        )


def set_setting(key: str, value: object, config_path: Path = CONFIG_PATH) -> Settings:
    """Update a single config key, persist, and emit telemetry.

    Reads the current settings, swaps in ``value`` for ``key``, and writes
    the result back. Emits ``config.set`` with the key, whether the value
    changed, and (for non-secret keys) the old and new values. Keys in
    ``SECRET_KEYS`` are redacted as ``***``.

    Args:
        key: Config field name (must be a valid ``Settings`` field).
        value: Parsed value to store.
        config_path: Path to the config file. Defaults to ``~/.mailpilot/config.json``.

    Returns:
        The updated ``Settings`` instance.

    Raises:
        KeyError: If ``key`` is not a valid Settings field.
    """
    if key not in Settings.model_fields:
        raise KeyError(key)
    current = load_settings(config_path=config_path)
    data = current.model_dump(mode="json")
    old_value = data.get(key)
    data[key] = value
    updated = Settings(**{k: v for k, v in data.items() if k in Settings.model_fields})
    save_settings(updated, config_path=config_path)
    new_value = updated.model_dump(mode="json").get(key)
    is_secret = key in SECRET_KEYS
    logfire.info(
        "config.set",
        key=key,
        changed=old_value != new_value,
        old=REDACTED if is_secret else old_value,
        new=REDACTED if is_secret else new_value,
    )
    return updated
