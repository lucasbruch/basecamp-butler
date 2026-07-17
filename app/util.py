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
