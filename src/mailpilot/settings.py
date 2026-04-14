"""Application settings stored as JSON in ~/.mailpilot/config.json."""

import json
from pathlib import Path

from pydantic import BaseModel, PostgresDsn

MAILPILOT_DIR = Path.home() / ".mailpilot"
CONFIG_PATH = MAILPILOT_DIR / "config.json"


DEFAULT_DATABASE_URL = "postgresql://localhost/mailpilot"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


class Settings(BaseModel):
    """MailPilot configuration."""

    database_url: PostgresDsn = PostgresDsn(DEFAULT_DATABASE_URL)
    logfire_token: str = ""
    logfire_environment: str = "development"
    anthropic_api_key: str = ""
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    google_project_id: str = ""
    google_pubsub_topic: str = "gmail-watch"
    google_pubsub_subscription: str = "mailpilot-watch"


def load_settings(config_path: Path = CONFIG_PATH) -> Settings:
    """Load settings from a JSON config file.

    Args:
        config_path: Path to the config file. Defaults to ~/.mailpilot/config.json.

    Returns:
        Settings with values from file, or defaults if file doesn't exist.
    """
    if not config_path.exists():
        defaults = Settings()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(defaults.model_dump(mode="json"), indent=2) + "\n"
        )
        return defaults

    data = json.loads(config_path.read_text())
    return Settings(**{k: v for k, v in data.items() if k in Settings.model_fields})


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
