"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Basecamp OAuth
    basecamp_client_id: str = ""
    basecamp_client_secret: str = ""
    basecamp_redirect_uri: str = "http://localhost:8000/oauth/callback"
    basecamp_user_agent: str = "BasecampButtler (set-a-contact@example.com)"

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Behaviour
    poll_interval_minutes: int = 7
    due_soon_days: int = 3

    # Classifier
    classifier: str = "rules"  # "rules" | "ollama"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.1:8b"

    # Database
    database_url: str = (
        "postgresql+psycopg2://basecamp:basecamp@localhost:5432/basecamp"
    )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


settings = Settings()
