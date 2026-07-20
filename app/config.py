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

    # Notifications
    notify_channel: str = "ntfy"  # "ntfy" | "telegram" | "none"

    # ntfy (default channel)
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""  # optional: for protected/self-hosted topics
    # Public base URL of THIS app, used to build notification action buttons,
    # e.g. http://192.168.1.50:8000  (leave blank to send buttonless messages)
    app_base_url: str = ""

    # Telegram (alternative channel)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Behaviour
    poll_interval_minutes: int = 5  # override with POLL_INTERVAL_MINUTES
    due_soon_days: int = 3
    # Extra sources beyond to-dos/messages/comments:
    poll_campfire: bool = True   # project group chat
    poll_pings: bool = True      # 1:1 / small-group direct messages (via notifications feed)

    # Classifier
    classifier: str = "rules"  # "rules" | "ollama"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.1:8b"
    # Optional outbound proxy for the Ollama calls only (Basecamp traffic and the
    # DB connection ignore it). Used when Ollama lives on another tailnet node the
    # container cannot route to directly: point this at a Tailscale userspace
    # sidecar's HTTP proxy, e.g. http://tailscale:1055. Blank = direct connection.
    ollama_proxy: str = ""

    # Web access control. Leave blank to keep the UI open (LAN-only default).
    # Set a secret to require it: browsers get an HTTP Basic prompt (any user,
    # this value as the password); the ntfy action buttons send it as a Bearer
    # token automatically. /healthz stays open for container health checks.
    web_auth_token: str = ""

    # Database
    database_url: str = (
        "postgresql+psycopg2://basecamp:basecamp@localhost:5432/basecamp"
    )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_topic)


settings = Settings()
