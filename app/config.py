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

    # Local time zone (IANA name, e.g. "Europe/Berlin"). Everything is stored in
    # UTC; this only affects how times are *displayed* and when the daily report
    # and quiet hours fire.
    timezone: str = "UTC"

    # Don't push individual alerts between these local hours (start inclusive,
    # end exclusive). Anything raised meanwhile is held and delivered as one
    # digest when quiet hours end. Set both to the same value to disable.
    quiet_hours_start: int = 22
    quiet_hours_end: int = 7

    # Collapse a cycle's suggestions into one push once it produces more than
    # this many. 0 disables digesting (always one push per suggestion).
    digest_threshold: int = 3

    # A burst of chat in one thread shouldn't raise a fresh suggestion every
    # poll. Within this many hours, an existing open suggestion for the same
    # thread suppresses a new one.
    thread_coalesce_hours: int = 6

    # Daily report (uses the same generator as the /report page).
    daily_report_enabled: bool = False
    daily_report_hour: int = 8     # local hour, 0-23
    daily_report_hours: int = 24   # size of the look-back window

    # Push confirmed suggestions back into Basecamp as real to-dos. Needs a
    # target to-do list picked per project on the Settings page.
    writeback_enabled: bool = False

    # Retention. raw_events is the fastest-growing table (one row per chat
    # line, with the full JSONB payload); todos keeps resolved items around for
    # history. 0 disables the sweep for that table.
    raw_event_retention_days: int = 90
    todo_retention_days: int = 180

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
