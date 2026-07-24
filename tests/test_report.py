"""The report module's pure helpers: hour clamping, the LLM digest, and the
deterministic fallback summary (all DB-free, exercised with plain objects)."""
from types import SimpleNamespace

from app import report


def _ev(event_id, etype, project_id, *, subject="", body="", who="", chat_id=None):
    payload = {}
    if subject:
        payload["subject"] = subject
    if body:
        payload["content"] = body
    if who:
        payload["creator"] = {"id": 1, "name": who}
    if chat_id is not None:
        payload["_chat_id"] = chat_id
    return SimpleNamespace(id=event_id, type=etype, project_id=project_id, payload=payload)


def test_clamp_hours_bounds_and_defaults():
    assert report.clamp_hours(10) == 10
    assert report.clamp_hours(0) == report.MIN_HOURS
    assert report.clamp_hours(9999) == report.MAX_HOURS
    assert report.clamp_hours("24") == 24
    assert report.clamp_hours("nonsense") == report.DEFAULT_HOURS
    assert report.clamp_hours(None) == report.DEFAULT_HOURS


def test_humanize_hours():
    assert report.humanize_hours(24) == "24 hours"
    assert report.humanize_hours(48) == "2 days"
    assert report.humanize_hours(1) == "1 hour"
    assert report.humanize_hours(10) == "10 hours"


def test_digest_groups_threads_and_lists_todos():
    events = [
        _ev(1, "message", 100, subject="Budget", body="please review", who="Ana"),
        _ev(2, "ping", 100, body="hey", who="Ben", chat_id=7),
        _ev(3, "ping", 100, body="can you send the deck", who="Ben", chat_id=7),
    ]
    todos = [SimpleNamespace(title="Send the deck to Ben")]
    names = {100: "Feature Film"}
    digest = report._build_digest(events, todos, names)

    # Single events render one line with project + sender.
    assert "[Feature Film] message by Ana — Budget: please review" in digest
    # Ping lines are folded into one thread block, in order.
    assert "[Feature Film] Ping thread:" in digest
    assert digest.index("Ben: hey") < digest.index("Ben: can you send the deck")
    # Raised to-dos are appended for context.
    assert "Send the deck to Ben" in digest


def test_digest_is_bounded():
    big = "x" * 500
    events = [_ev(i, "message", 100, body=big, who="Ana") for i in range(200)]
    digest = report._build_digest(events, [], {100: "P"})
    assert len(digest) <= report.MAX_DIGEST_CHARS + len("\n… (truncated)")
    assert digest.endswith("… (truncated)")


def test_fallback_report_is_structured_and_short():
    events = [
        _ev(1, "message", 100, subject="Budget", who="Ana"),
        _ev(2, "comment", 100, body="looks good", who="Ben"),
        _ev(3, "ping", 200, body="ping me", who="Cara", chat_id=9),
    ]
    todos = [SimpleNamespace(title="Reply to Cara")]
    names = {100: "Film", 200: "Ad"}
    counts = report._count_types(events)
    text = report._fallback_report(events, todos, names, counts, "24 hours")

    assert text.startswith("TL;DR:")
    assert "2 project(s)" in text  # 100 and 200
    assert "Needs your attention:" in text
    assert "Reply to Cara" in text
    assert "Highlights:" in text
    # Per-type breakdown line present.
    assert "Messages: 1" in text and "Pings: 1" in text
