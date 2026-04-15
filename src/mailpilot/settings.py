"""Application settings with env var overrides and JSON file persistence.

Priority (highest to lowest):
1. Constructor kwargs (for tests)
2. ``MAILPILOT_*`` environment variables
3. ``~/.mailpilot/config.json`` file
4. Field defaults
"""

import json
from pathlib import Path
from typing import Any

from pydantic import PostgresDsn
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

MAILPILOT_DIR = Path.home() / ".mailpilot"
CONFIG_PATH = MAILPILOT_DIR / "config.json"

DEFAULT_DATABASE_URL = "postgresql://localhost/mailpilot"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


class JsonConfigSource(PydanticBaseSettingsSource):
    """Load settings from ~/.mailpilot/config.json."""

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Not used -- __call__ returns the full dict."""
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        """Read config file and return known fields."""
        if not CONFIG_PATH.exists():
            return {}
        data: dict[str, Any] = json.loads(CONFIG_PATH.read_text())
        return {k: v for k, v in data.items() if k in self.settings_cls.model_fields}


class Settings(BaseSettings):
    """MailPilot configuration."""

    model_config = SettingsConfigDict(env_prefix="MAILPILOT_")

    database_url: PostgresDsn = PostgresDsn(DEFAULT_DATABASE_URL)
    logfire_token: str = ""
    logfire_environment: str = "development"
    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    google_project_id: str = ""
    google_pubsub_topic: str = "gmail-watch"
    google_pubsub_subscription: str = "mailpilot-watch"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Set source priority: kwargs > env vars > config file."""
        return (
            init_settings,
            env_settings,
            JsonConfigSource(settings_cls),
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
            json.dumps(defaults.model_dump(mode="json"), indent=2) + "\n"
        )
        return defaults

    if config_path == CONFIG_PATH:
        return Settings()

    # Non-default path: read file directly and pass as kwargs so
    # JsonConfigSource (which hardcodes CONFIG_PATH) is bypassed.
    data: dict[str, Any] = json.loads(config_path.read_text())
    overrides = {k: v for k, v in data.items() if k in Settings.model_fields}
    return Settings(**overrides)


def save_settings(settings: Settings, config_path: Path = CONFIG_PATH) -> None:
    """Save settings to a JSON config file.

    Args:
        settings: Settings to save.
        config_path: Path to the config file. Defaults to ~/.mailpilot/config.json.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(settings.model_dump(mode="json"), indent=2)
    config_path.write_text(data + "\n")


def get_settings() -> Settings:
    """Load settings from the default config path."""
    return load_settings()
