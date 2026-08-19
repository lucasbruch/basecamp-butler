"""Small shared helpers."""
from __future__ import annotations

import html
from datetime import date, datetime, timezone, tzinfo

# Every column a `safe_url` result is stored in is a String(1000).
MAX_URL = 1000


def parse_bc_datetime(value: str | None) -> datetime | None:
    """Parse a Basecamp ISO-8601 timestamp (e.g. '2024-01-02T15:04:05.000Z').

    Raises ValueError on a string that isn't one — callers that read from a
    payload or a hand-editable `app_state` row catch that and carry on. A value
    that isn't a string at all is the same kind of "unusable input", so it
    raises the same exception rather than an AttributeError those callers
    don't (and shouldn't have to) catch.
    """
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError(f"not a timestamp string: {value!r}")
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
    allowlist the scheme rather than trust the payload.

    A link longer than `MAX_URL` is dropped too. Every column these land in is
    a String(1000), so an over-long value doesn't produce a bad link — it
    produces a failed INSERT that takes the whole poll or classify pass down
    with it. Truncating would only turn one broken link into another, so the
    honest answer is no link at all."""
    if not url:
        return None
    u = url.strip()
    low = u.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    return u if len(u) <= MAX_URL else None


def as_html(text: str) -> str:
    """Turn plain text into the rich text Basecamp expects.

    Chat lines and to-do descriptions are both HTML. What goes into them here is
    written by a language model, typed into a form, or lifted out of somebody
    else's message, so the three characters that carry structure —
    ``&``, ``<`` and ``>`` — are escaped rather than trusted: otherwise a ``<``
    in the text silently swallows the rest of it, and anything more deliberate
    could be echoed back as live markup.

    Quotes and apostrophes are deliberately left alone. They only need escaping
    inside an attribute value, and we never build one — while Basecamp does not
    decode the entity on the way back out, so escaping them puts the entity
    itself in front of the reader: "it&#x27;s" where you wrote "it's".
    """
    escaped = html.escape(text.strip(), quote=False)
    return "<br>".join(line for line in escaped.split("\n"))
