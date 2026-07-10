"""Regression guard for the critical bug where the recurring poll job was added
with next_run_time=None (which APScheduler treats as *paused*), so Basecamp was
only ever polled once at boot and never again."""
from apscheduler.schedulers.background import BackgroundScheduler

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
    assert {"poll", "reminders", "classify", "poll-now"} <= ids
