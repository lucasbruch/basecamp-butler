"""Small shared helpers."""
from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo


def parse_bc_datetime(value: str | None) -> datetime | None:
    """Parse a Basecamp ISO-8601 timestamp (e.g. '2024-01-02T15:04:05.000Z')."""
    if not value:
        return None
    # Normalise the trailing 'Z' to +00:00 — fromisoformat only accepts 'Z'
    # natively from Python 3.11 on, and we want to be version-independent.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: datetime | None) -> datetime | None:
    """Treat a naive datetime as UTC. Everything is *written* as UTC, but a naive
    value can still come back — from a row predating the timezone-aware columns,
    or a driver that drops the offset."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def due_on(due: datetime | None, tz: tzinfo, *, all_day: bool = False) -> date | None:
    """The calendar date a to-do is due, as the user would say it out loud.

    Two kinds of value share the `due_date` column. A Basecamp `due_on` is a bare
    date that we store as midnight UTC — it names a day, not an instant, so
    converting it to a zone behind UTC would slide it onto the day before.
    A schedule entry's `starts_at` is a real instant, and *must* be converted or
    a 23:30 UTC meeting shows up on the wrong day in Berlin. `all_day` says which
    one we're holding."""
    due = as_aware(due)
    if due is None:
        return None
    return due.date() if all_day else due.astimezone(tz).date()


def safe_url(url: str | None) -> str | None:
    """Return `url` only if it's an http(s) link; otherwise None.

    Deep links come from Basecamp payloads and are rendered as href attributes.
    A hostile value like ``javascript:alert(1)`` would execute on click, so we
    allowlist the scheme rather than trust the payload."""
    if not url:
        return None
    u = url.strip()
    low = u.lower()
    return u if low.startswith("http://") or low.startswith("https://") else None
