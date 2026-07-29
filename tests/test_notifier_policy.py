"""When to interrupt someone: quiet hours and digesting.

Both exist because an assistant that buzzes eleven times at 3am gets muted, and
a muted assistant is worth nothing.
"""
from datetime import datetime, timezone

import pytest

from app import notifier, runtime


def cfg(**over):
    return runtime.RuntimeConfig(**{**runtime.defaults(), "timezone": "UTC", **over})


@pytest.fixture
def spy(monkeypatch):
    """Capture what the notifier decided to do, without touching a network."""
    calls = {"individual": [], "digest": [], "held": []}

    monkeypatch.setattr(notifier, "notify_new_todo",
                        lambda tid, c=None: calls["individual"].append(tid))
    monkeypatch.setattr(notifier, "_send_digest",
                        lambda titles, c, prefix="": calls["digest"].append(list(titles)))
    monkeypatch.setattr(notifier, "hold", lambda ids: calls["held"].extend(ids))
    return calls


def use(monkeypatch, config):
    monkeypatch.setattr(notifier.runtime, "current", lambda: config)


def test_a_small_batch_sends_individually(monkeypatch, spy):
    use(monkeypatch, cfg(digest_threshold=3, quiet_hours_start=0, quiet_hours_end=0))
    notifier.dispatch([1, 2])
    assert spy["individual"] == [1, 2]
    assert not spy["digest"]


def test_a_flood_is_digested(monkeypatch, spy):
    """Five suggestions from one Campfire burst is one notification, not five."""
    use(monkeypatch, cfg(digest_threshold=3, quiet_hours_start=0, quiet_hours_end=0))

    # `dispatch` reads the titles back from the DB; stub that lookup out.
    monkeypatch.setattr(notifier, "session_scope", _fake_scope([]))
    notifier.dispatch([1, 2, 3, 4, 5])
    assert spy["individual"] == []


def test_threshold_zero_never_digests(monkeypatch, spy):
    use(monkeypatch, cfg(digest_threshold=0, quiet_hours_start=0, quiet_hours_end=0))
    notifier.dispatch([1, 2, 3, 4, 5, 6, 7])
    assert spy["individual"] == [1, 2, 3, 4, 5, 6, 7]


def test_quiet_hours_hold_everything(monkeypatch, spy):
    quiet = cfg(quiet_hours_start=0, quiet_hours_end=23)  # almost always quiet
    use(monkeypatch, quiet)
    assert quiet.is_quiet_now(datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc))
    notifier.dispatch([1, 2])
    assert spy["individual"] == []
    assert spy["held"] == [1, 2]


def test_channel_none_sends_nothing(monkeypatch, spy):
    use(monkeypatch, cfg(notify_channel="none"))
    notifier.dispatch([1, 2, 3])
    assert spy == {"individual": [], "digest": [], "held": []}


def test_empty_batch_is_a_no_op(monkeypatch, spy):
    use(monkeypatch, cfg())
    notifier.dispatch([])
    assert spy == {"individual": [], "digest": [], "held": []}


# ── the held queue ───────────────────────────────────────────────────────────
def test_held_ids_round_trip(db):
    notifier._write_held(db, [3, 1, 2])
    assert notifier._read_held(db) == [3, 1, 2]


def test_held_queue_survives_junk(db):
    from app.models import AppState

    db.merge(AppState(key=notifier.HELD_KEY, value="1,,banana,3"))
    db.flush()
    assert notifier._read_held(db) == [1, 3]


def test_held_queue_is_capped(db, monkeypatch):
    monkeypatch.setattr(notifier, "MAX_HELD", 5)
    notifier._write_held(db, list(range(100)))
    assert notifier._read_held(db) == [95, 96, 97, 98, 99]


def _fake_scope(rows):
    """A session_scope stand-in whose execute() yields `rows`."""
    from contextlib import contextmanager

    class _Result:
        def scalars(self):
            return rows

    class _DB:
        def execute(self, *a, **k):
            return _Result()

    @contextmanager
    def scope():
        yield _DB()

    return scope
