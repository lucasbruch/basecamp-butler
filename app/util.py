"""Small shared helpers."""
from __future__ import annotations

from datetime import datetime, timezone


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
