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
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

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
    autoreply_enabled: bool
    autoreply_cooldown_minutes: int
    autoreply_daily_limit: int
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
    "timezone": ("tz", ()),
    "quiet_hours_start": ("int", ()),
    "quiet_hours_end": ("int", ()),
    "digest_threshold": ("int", ()),
    "thread_coalesce_hours": ("int", ()),
    "daily_report_enabled": ("bool", ()),
    "daily_report_hour": ("int", ()),
    "daily_report_hours": ("int", ()),
    "writeback_enabled": ("bool", ()),
    "autoreply_enabled": ("bool", ()),
    "autoreply_cooldown_minutes": ("int", ()),
    "autoreply_daily_limit": ("int", ()),
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
    "autoreply_cooldown_minutes": (0, 1440),
    "autoreply_daily_limit": (0, 200),
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


def valid_tz(name: str) -> bool:
    """Whether `name` is a zone this machine actually knows.

    Note `ZoneInfo` is case-sensitive and slash-delimited: 'europe/berlin' and
    'Europe\\Berlin' are both unknown. The Settings page offers a dropdown so
    neither is reachable from the UI, but the POST route and the `TIMEZONE`
    environment variable both still take a raw string."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


# Legacy aliases and fixed-offset pseudo-zones. Every one of these duplicates a
# real zone or (in Etc's case) has a sign convention that reads backwards, so
# they'd be four hundred lines of noise in a picker. Anything already set is
# still offered — see `zone_choices`.
_ZONE_ALIASES = (
    "Etc/", "SystemV/", "US/", "Canada/", "Brazil/", "Chile/", "Mexico/",
)


@lru_cache(maxsize=1)
def available_zones() -> tuple[str, ...]:
    """Every canonical IANA zone this machine knows, UTC first.

    Cached: it walks the whole tzdata package, and the answer only changes when
    the package does — i.e. on a redeploy."""
    try:
        names = available_timezones()
    except Exception:  # pragma: no cover — a tzdata-less image
        log.exception("Could not enumerate time zones — offering UTC only.")
        return ("UTC",)
    keep = {n for n in names if "/" in n and not n.startswith(_ZONE_ALIASES)}
    return ("UTC", *sorted(keep))


def zone_choices(current: str | None = None) -> tuple[str, ...]:
    """The picker's options, always including whatever is set right now.

    A `<select>` submits one of its own options, so a value missing from the
    list would be silently rewritten the next time anyone saved the form. If
    someone's `TIMEZONE` is a legacy alias we filtered out, it stays on offer."""
    zones = set(available_zones())
    if current:
        zones.add(current)
    zones.discard("UTC")
    return ("UTC", *sorted(zones))


def zone_groups(current: str | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """`zone_choices` arranged as (continent, [(value, label), ...]) for optgroups.

    Labels drop the continent prefix the group already shows, which also makes
    the browser's type-ahead match on the city — "Berl" finds Berlin."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for name in zone_choices(current):
        region, _, rest = name.partition("/")
        groups.setdefault(region, []).append((name, (rest or name).replace("_", " ")))
    return list(groups.items())


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
    if kind == "tz":
        name = raw.strip()
        if not valid_tz(name):
            # Raising (rather than silently substituting UTC) is what lets `save`
            # refuse the write and the Settings page say so. A zone that quietly
            # doesn't apply is worse than one that visibly won't.
            raise ValueError(f"unknown time zone {name!r}")
        return name
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


def save(
    db: Session, updates: dict[str, Any], rejected: set[str] | None = None
) -> RuntimeConfig:
    """Persist overrides. A value equal to the env default clears the override
    instead of pinning it, so the environment stays meaningful.

    A value that won't coerce is left unwritten — the previous setting stands.
    Pass `rejected` to find out which keys those were, so the caller can tell the
    user rather than bouncing them back to a page that looks like it saved."""
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
            if rejected is not None:
                rejected.add(key)
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
