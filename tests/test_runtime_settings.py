"""Runtime settings: env defaults, database overrides, bounds, and the
quiet-hours window that has to wrap midnight to be worth anything."""
from datetime import datetime, timezone

import pytest

from app import runtime
from app.config import settings
from app.models import AppState


def test_defaults_come_from_the_environment(db):
    cfg = runtime.load(db)
    assert cfg.poll_interval_minutes == settings.poll_interval_minutes
    assert cfg.classifier == settings.classifier


def test_an_override_wins(db):
    runtime.save(db, {"poll_interval_minutes": "15"})
    assert runtime.load(db).poll_interval_minutes == 15


def test_saving_the_env_default_clears_the_override(db):
    """Otherwise changing the environment variable later would appear to do
    nothing, because a stale row was silently pinning the old value."""
    runtime.save(db, {"poll_interval_minutes": "15"})
    assert "poll_interval_minutes" in runtime.overrides(db)

    runtime.save(db, {"poll_interval_minutes": str(settings.poll_interval_minutes)})
    assert "poll_interval_minutes" not in runtime.overrides(db)
    assert runtime.load(db).poll_interval_minutes == settings.poll_interval_minutes


def test_out_of_range_values_are_clamped(db):
    runtime.save(db, {"poll_interval_minutes": "0"})
    assert runtime.load(db).poll_interval_minutes == 1
    runtime.save(db, {"poll_interval_minutes": "99999"})
    assert runtime.load(db).poll_interval_minutes == 1440


def test_a_hand_edited_junk_row_is_ignored(db):
    db.merge(AppState(key="cfg_poll_interval_minutes", value="banana"))
    db.flush()
    assert runtime.load(db).poll_interval_minutes == settings.poll_interval_minutes


def test_unknown_choice_falls_back(db):
    runtime.save(db, {"classifier": "telepathy"})
    assert runtime.load(db).classifier == settings.classifier


def test_booleans_round_trip(db):
    runtime.save(db, {"writeback_enabled": "true"})
    assert runtime.load(db).writeback_enabled is True
    runtime.save(db, {"writeback_enabled": "false"})
    assert runtime.load(db).writeback_enabled is False


def test_unknown_keys_are_ignored(db):
    runtime.save(db, {"rm_rf_slash": "yes"})
    assert "rm_rf_slash" not in runtime.overrides(db)


# ── quiet hours ──────────────────────────────────────────────────────────────
def _cfg(**over):
    return runtime.RuntimeConfig(**{**runtime.defaults(), **over})


def at(hour, tz="UTC"):
    return datetime(2026, 7, 29, hour, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("hour,quiet", [
    (21, False), (22, True), (23, True), (0, True), (3, True), (6, True), (7, False), (12, False),
])
def test_window_wraps_midnight(hour, quiet):
    """22:00→07:00 is the whole point; a naive start <= h < end comparison is
    wrong for exactly the hours people actually configure."""
    cfg = _cfg(quiet_hours_start=22, quiet_hours_end=7, timezone="UTC")
    assert cfg.is_quiet_now(at(hour)) is quiet


@pytest.mark.parametrize("hour,quiet", [
    (0, False), (9, True), (12, True), (16, False), (20, False),
])
def test_a_daytime_window_does_not_wrap(hour, quiet):
    cfg = _cfg(quiet_hours_start=9, quiet_hours_end=16, timezone="UTC")
    assert cfg.is_quiet_now(at(hour)) is quiet


def test_equal_bounds_disable_the_window():
    cfg = _cfg(quiet_hours_start=8, quiet_hours_end=8, timezone="UTC")
    assert not cfg.quiet_hours_active
    assert cfg.is_quiet_now(at(8)) is False


def test_quiet_hours_are_evaluated_in_local_time():
    # 23:00 UTC is 01:00 in Berlin (CEST, +02:00) — inside a 22→07 window there
    # but outside one anchored to UTC would be a different answer entirely.
    berlin = _cfg(quiet_hours_start=2, quiet_hours_end=3, timezone="Europe/Berlin")
    assert berlin.is_quiet_now(at(0)) is True   # 02:00 local
    assert berlin.is_quiet_now(at(3)) is False  # 05:00 local


def test_unknown_timezone_falls_back_to_utc():
    cfg = _cfg(timezone="Mars/Olympus_Mons")
    assert cfg.tz.key == "UTC"
