"""Single-process entrypoint: web UI + poll scheduler + reminder sweep + Telegram listener.

Run with:  python -m app.main
(That's the container's default CMD.)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

from .classifier import classify_new_events
from .config import settings
from .db import init_db
from .notifier import send_due_reminders, start_listener
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


def schedule_jobs(sched) -> int:
    """Register the recurring jobs + the one-off boot poll. Returns the interval.

    Kept as a standalone function (not inlined in lifespan) so it can be unit
    tested — a regression here silently stops the app from ever polling.
    """
    interval = max(1, settings.poll_interval_minutes)
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
    # Kick an immediate poll shortly after boot instead of waiting a full interval.
    sched.add_job(_safe_poll, "date", id="poll-now")
    return interval


@asynccontextmanager
async def lifespan(app):
    init_db()
    log.info("Database ready.")

    start_listener()  # no-op if Telegram unconfigured

    interval = schedule_jobs(scheduler)
    scheduler.start()
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
