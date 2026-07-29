"""Settings you can change from the Settings page, without a redeploy.

The env vars in `config.Settings` stay the source of *defaults*. Anything in
here can additionally be overridden at runtime; the override lives in
``app_state`` under a ``cfg_`` prefix and wins whenever it's present.

Why this exists: the app's whole deployment story is "a Portainer stack on a
NAS". Changing the poll interval or turning on the LLM meant editing an
environment variable and redeploying the stack — minutes of downtime to flip a
boolean. The assistant persona already worked this way; this generalises it.

Reading is deliberately cheap: `load(db)` does one query for all overrides and
returns an immutable snapshot. Call it once per operation and pass the snapshot
around rather than re-reading per field.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AppState

log = logging.getLogger(__name__)

PREFIX = "cfg_"

CLASSIFIERS = ("rules", "ollama")
CHANNELS = ("ntfy", "telegram", "none")


@dataclass(frozen=True)
class RuntimeConfig:
    """An immutable snapshot of the effective settings."""

    poll_interval_minutes: int
    due_soon_days: int
    classifier: str
    notify_channel: str
    timezone: str
    quiet_hours_start: int
    quiet_hours_end: int
    digest_threshold: int
    thread_coalesce_hours: int
    daily_report_enabled: bool
    daily_report_hour: int
    daily_report_hours: int
    writeback_enabled: bool
    raw_event_retention_days: int
    todo_retention_days: int

    @property
    def tz(self) -> tzinfo:
        return resolve_tz(self.timezone)

    @property
    def quiet_hours_active(self) -> bool:
        """False when the window is empty (start == end), which disables it."""
        return self.quiet_hours_start != self.quiet_hours_end

    def is_quiet_now(self, now: datetime | None = None) -> bool:
        """True if `now` (UTC) falls inside the local quiet-hours window.

        The window wraps midnight in the common case (22:00 → 07:00), so a plain
        ``start <= hour < end`` comparison would be wrong exactly when people
        actually use it."""
        if not self.quiet_hours_active:
            return False
        local = (now or datetime.now(self.tz)).astimezone(self.tz)
        hour = local.hour
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end


# key -> (kind, choices). The default always comes from `config.settings`, so a
# field added to the env config is picked up here by name.
_SPEC: dict[str, tuple[str, tuple[str, ...]]] = {
    "poll_interval_minutes": ("int", ()),
    "due_soon_days": ("int", ()),
    "classifier": ("choice", CLASSIFIERS),
    "notify_channel": ("choice", CHANNELS),
    "timezone": ("str", ()),
    "quiet_hours_start": ("int", ()),
    "quiet_hours_end": ("int", ()),
    "digest_threshold": ("int", ()),
    "thread_coalesce_hours": ("int", ()),
    "daily_report_enabled": ("bool", ()),
    "daily_report_hour": ("int", ()),
    "daily_report_hours": ("int", ()),
    "writeback_enabled": ("bool", ()),
    "raw_event_retention_days": ("int", ()),
    "todo_retention_days": ("int", ()),
}

# Sane bounds, applied on write *and* on read — a hand-edited app_state row
# shouldn't be able to set a 0-minute poll interval.
_BOUNDS: dict[str, tuple[int, int]] = {
    "poll_interval_minutes": (1, 1440),
    "due_soon_days": (0, 60),
    "quiet_hours_start": (0, 23),
    "quiet_hours_end": (0, 23),
    "digest_threshold": (0, 100),
    "thread_coalesce_hours": (0, 168),
    "daily_report_hour": (0, 23),
    "daily_report_hours": (1, 72),
    "raw_event_retention_days": (0, 3650),
    "todo_retention_days": (0, 3650),
}

KEYS = tuple(_SPEC)


def resolve_tz(name: str | None) -> tzinfo:
    """A tzinfo for an IANA name, falling back to UTC on anything unusable."""
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("Unknown time zone %r — using UTC.", name)
        return ZoneInfo("UTC")


def _coerce(key: str, raw: str) -> Any:
    kind, choices = _SPEC[key]
    if kind == "int":
        value = int(raw)
        if key in _BOUNDS:
            low, high = _BOUNDS[key]
            value = max(low, min(high, value))
        return value
    if kind == "bool":
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if kind == "choice":
        return raw if raw in choices else getattr(settings, key)
    return raw.strip()


def defaults() -> dict[str, Any]:
    """The env-configured values, before any runtime override."""
    return {key: getattr(settings, key) for key in KEYS}


def load(db: Session) -> RuntimeConfig:
    """One query for every override; returns the effective snapshot."""
    values = defaults()
    rows = db.execute(
        select(AppState).where(AppState.key.like(f"{PREFIX}%"))
    ).scalars()
    for row in rows:
        key = row.key[len(PREFIX):]
        if key not in _SPEC or row.value is None or row.value == "":
            continue
        try:
            values[key] = _coerce(key, row.value)
        except (TypeError, ValueError):
            log.warning("Ignoring unusable override %s=%r", row.key, row.value)
    return RuntimeConfig(**values)


def overrides(db: Session) -> dict[str, Any]:
    """Only the keys that are actually overridden — lets the Settings page show
    "inherited from the environment" versus "set here"."""
    rows = db.execute(
        select(AppState).where(AppState.key.like(f"{PREFIX}%"))
    ).scalars()
    out: dict[str, Any] = {}
    for row in rows:
        key = row.key[len(PREFIX):]
        if key in _SPEC and row.value not in (None, ""):
            out[key] = row.value
    return out


def save(db: Session, updates: dict[str, Any]) -> RuntimeConfig:
    """Persist overrides. A value equal to the env default clears the override
    instead of pinning it, so the environment stays meaningful."""
    env = defaults()
    for key, raw in updates.items():
        if key not in _SPEC:
            continue
        text = "" if raw is None else str(raw).strip()
        if text == "":
            db.merge(AppState(key=PREFIX + key, value=""))
            continue
        try:
            value = _coerce(key, text)
        except (TypeError, ValueError):
            continue
        db.merge(
            AppState(key=PREFIX + key, value="" if value == env[key] else str(value))
        )
    db.flush()
    return load(db)


def current() -> RuntimeConfig:
    """Snapshot for callers that don't already hold a session.

    Opens its own short read transaction. Prefer `load(db)` where a session is
    already in hand."""
    from .db import session_scope

    with session_scope() as db:
        return load(db)
