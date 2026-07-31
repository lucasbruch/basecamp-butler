"""Regression guard for the critical bug where the recurring poll job was added
with next_run_time=None (which APScheduler treats as *paused*), so Basecamp was
only ever polled once at boot and never again."""
from datetime import timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app import main
from app.main import schedule_jobs


def test_poll_job_is_not_paused():
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)  # compute next_run_time without actually firing
        poll = sched.get_job("poll")
        assert poll is not None
        # The interval poll MUST have a scheduled next run; None means paused.
        assert poll.next_run_time is not None
        # And the boot one-shot should exist too.
        assert sched.get_job("poll-now") is not None
    finally:
        sched.shutdown(wait=False)


def test_all_recurring_jobs_registered():
    sched = BackgroundScheduler(timezone="UTC")
    interval = schedule_jobs(sched)
    assert interval >= 1
    ids = {j.id for j in sched.get_jobs()}
    assert {"poll", "reminders", "classify", "apply-settings", "poll-now"} <= ids


def test_interval_change_reschedules_the_poll(monkeypatch):
    """Changing the poll cadence on the Settings page has to take effect without
    a stack redeploy, which is the whole point of the runtime settings."""
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)

        monkeypatch.setattr(
            main.runtime, "current",
            lambda: main.runtime.RuntimeConfig(
                **{**main.runtime.defaults(), "poll_interval_minutes": 17}
            ),
        )
        main.apply_settings(sched)
        assert sched.get_job("poll").trigger.interval.total_seconds() == 17 * 60
    finally:
        sched.shutdown(wait=False)


def test_daily_report_job_appears_and_disappears(monkeypatch):
    monkeypatch.setattr(main, "_daily_report_spec", None)
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)

        def use(**over):
            monkeypatch.setattr(
                main.runtime, "current",
                lambda: main.runtime.RuntimeConfig(
                    **{**main.runtime.defaults(), **over}
                ),
            )

        use(daily_report_enabled=True, daily_report_hour=8, timezone="Europe/Berlin")
        main.apply_settings(sched)
        job = sched.get_job("daily-report")
        assert job is not None
        assert str(job.trigger.timezone) == "Europe/Berlin"

        use(daily_report_enabled=False)
        main.apply_settings(sched)
        assert sched.get_job("daily-report") is None
    finally:
        sched.shutdown(wait=False)


def test_daily_report_fires_at_the_local_wall_clock(monkeypatch):
    """"08:00" has to mean 08:00 where the user is, in whichever half of the year
    they happen to be — the scheduler itself runs on UTC, so this is the bit that
    would silently drift by an hour every March."""
    monkeypatch.setattr(main, "_daily_report_spec", None)
    monkeypatch.setattr(
        main.runtime, "current",
        lambda: main.runtime.RuntimeConfig(
            **{**main.runtime.defaults(), "daily_report_enabled": True,
               "daily_report_hour": 8, "timezone": "Europe/Berlin"}
        ),
    )
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)
        main.apply_settings(sched)
        berlin = ZoneInfo("Europe/Berlin")
        nxt = sched.get_job("daily-report").next_run_time
        assert nxt.astimezone(berlin).hour == 8
        # ...and the UTC instant it maps to is 06:00 in summer, 07:00 in winter.
        assert nxt.astimezone(timezone.utc).hour == (
            6 if nxt.astimezone(berlin).dst() else 7
        )
    finally:
        sched.shutdown(wait=False)


def test_changing_only_the_timezone_reschedules_the_report(monkeypatch):
    """The cron is rebuilt from a (hour, timezone) pair. Leaving the hour alone
    and moving the zone still has to re-point the job."""
    monkeypatch.setattr(main, "_daily_report_spec", None)
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)

        def use(tz):
            monkeypatch.setattr(
                main.runtime, "current",
                lambda: main.runtime.RuntimeConfig(
                    **{**main.runtime.defaults(), "daily_report_enabled": True,
                       "daily_report_hour": 8, "timezone": tz}
                ),
            )

        use("UTC")
        main.apply_settings(sched)
        utc_run = sched.get_job("daily-report").next_run_time

        use("Europe/Berlin")
        main.apply_settings(sched)
        berlin_run = sched.get_job("daily-report").next_run_time

        assert str(sched.get_job("daily-report").trigger.timezone) == "Europe/Berlin"
        assert berlin_run != utc_run
    finally:
        sched.shutdown(wait=False)


def test_daily_report_is_not_rescheduled_when_nothing_changed(monkeypatch):
    """apply_settings runs every minute; it must be a no-op in the steady state
    rather than resetting the cron's next fire time on every pass."""
    monkeypatch.setattr(main, "_daily_report_spec", None)
    monkeypatch.setattr(
        main.runtime, "current",
        lambda: main.runtime.RuntimeConfig(
            **{**main.runtime.defaults(), "daily_report_enabled": True,
               "daily_report_hour": 8}
        ),
    )
    sched = BackgroundScheduler(timezone="UTC")
    try:
        schedule_jobs(sched)
        sched.start(paused=True)
        main.apply_settings(sched)
        first = sched.get_job("daily-report").next_run_time
        main.apply_settings(sched)
        assert sched.get_job("daily-report").next_run_time == first
    finally:
        sched.shutdown(wait=False)


def test_schedule_jobs_survives_an_unreachable_database(monkeypatch):
    """Boot must not depend on Postgres already answering — the app schedules
    its jobs before the first successful query."""
    def boom():
        raise RuntimeError("no database yet")

    monkeypatch.setattr(main.runtime, "current", boom)
    sched = BackgroundScheduler(timezone="UTC")
    assert schedule_jobs(sched) >= 1
    assert sched.get_job("poll") is not None
