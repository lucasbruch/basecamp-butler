"""Single-process entrypoint: web UI + poll scheduler + reminder sweep + Telegram listener.

Run with:  python -m app.main
(That's the container's default CMD.)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from . import runtime
from .classifier import classify_new_events
from .db import init_db
from .notifier import flush_held, send_due_reminders, start_listener
from .poller.poller import run_poll_cycle
from .web.routes import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("app")

scheduler = BackgroundScheduler(timezone="UTC")


def _safe_poll() -> None:
    try:
        run_poll_cycle()
    except Exception:
        log.exception("Poll cycle failed")


def _safe_reminders() -> None:
    try:
        send_due_reminders()
    except Exception:
        log.exception("Reminder sweep failed")
    try:
        # Anything held back during quiet hours goes out once the window ends.
        flush_held()
    except Exception:
        log.exception("Held-notification flush failed")


def _safe_classify() -> None:
    """Standalone classify pass, decoupled from polling.

    Lets a backlog left by an unreachable LLM drain within ~1 min of the LLM
    coming back, instead of waiting for the next successful poll. It's a no-op
    when there's nothing unprocessed, and it can't overlap the poll's own
    classify call (classify_new_events is lock-guarded).
    """
    try:
        classify_new_events()
    except Exception:
        log.exception("Classification sweep failed")


def _safe_daily_report() -> None:
    from . import report

    try:
        report.run_daily()
    except Exception:
        log.exception("Daily report failed")


def _safe_apply_settings() -> None:
    """Re-read the runtime settings and re-point the jobs that depend on them.

    Poll cadence and the daily-report time are editable from the Settings page,
    and a NAS user changing them expects the change to take, not to require a
    stack redeploy. This runs every minute and is a no-op unless something moved.
    """
    try:
        apply_settings(scheduler)
    except Exception:
        log.exception("Could not apply updated settings")


# The (hour, timezone) the daily-report cron is currently built for. APScheduler
# Job objects use __slots__, so this can't be stashed on the job itself, and
# re-deriving it from the trigger's cron fields is more fragile than it's worth.
_daily_report_spec: tuple[int, str] | None = None


def apply_settings(sched) -> None:
    """Reconcile interval/cron jobs with the current runtime config."""
    global _daily_report_spec
    cfg = runtime.current()

    poll = sched.get_job("poll")
    interval = max(1, cfg.poll_interval_minutes)
    if poll is not None and getattr(poll.trigger, "interval", None) is not None:
        if poll.trigger.interval.total_seconds() != interval * 60:
            log.info("Poll interval changed to %d min — rescheduling.", interval)
            sched.reschedule_job("poll", trigger="interval", minutes=interval)

    daily = sched.get_job("daily-report")
    if cfg.daily_report_enabled:
        want = (cfg.daily_report_hour, cfg.timezone)
        if daily is None or _daily_report_spec != want:
            log.info(
                "Daily report scheduled for %02d:00 %s.", cfg.daily_report_hour, cfg.timezone
            )
            sched.add_job(
                _safe_daily_report,
                "cron",
                id="daily-report",
                hour=cfg.daily_report_hour,
                minute=0,
                timezone=cfg.tz,
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            _daily_report_spec = want
    elif daily is not None:
        sched.remove_job("daily-report")
        _daily_report_spec = None


def schedule_jobs(sched) -> int:
    """Register the recurring jobs + the one-off boot poll. Returns the interval.

    Kept as a standalone function (not inlined in lifespan) so it can be unit
    tested — a regression here silently stops the app from ever polling.
    """
    cfg = runtime_or_env()
    interval = max(1, cfg.poll_interval_minutes)
    # NB: do NOT pass next_run_time=None here — in APScheduler that adds the job
    # *paused*, so the interval never fires. Omitting it lets the trigger compute
    # the first run at now+interval; the "poll-now" job below covers boot.
    sched.add_job(
        _safe_poll, "interval", minutes=interval, id="poll",
        max_instances=1, coalesce=True,
    )
    sched.add_job(
        _safe_reminders, "interval", minutes=1, id="reminders",
        max_instances=1, coalesce=True,
    )
    sched.add_job(
        _safe_classify, "interval", minutes=1, id="classify",
        max_instances=1, coalesce=True,
    )
    sched.add_job(
        _safe_apply_settings, "interval", minutes=1, id="apply-settings",
        max_instances=1, coalesce=True,
    )
    # Kick an immediate poll shortly after boot instead of waiting a full interval.
    sched.add_job(_safe_poll, "date", id="poll-now")
    return interval


def runtime_or_env() -> runtime.RuntimeConfig:
    """Runtime config, tolerating a database that isn't reachable yet.

    `schedule_jobs` runs at boot and in tests, neither of which can assume a
    live Postgres; falling back to the env defaults keeps both working.
    """
    try:
        return runtime.current()
    except Exception:
        log.warning("Could not read runtime settings — using environment defaults.")
        return runtime.RuntimeConfig(**runtime.defaults())


@asynccontextmanager
async def lifespan(app):
    init_db()
    log.info("Database ready.")

    start_listener()  # no-op if Telegram unconfigured

    interval = schedule_jobs(scheduler)
    scheduler.start()
    apply_settings(scheduler)  # picks up the daily report if it's enabled
    log.info("Scheduler started — polling every %d min.", interval)

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = create_app()
app.router.lifespan_context = lifespan


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    main()
