"""Single-process entrypoint: web UI + poll scheduler + reminder sweep + Telegram listener.

Run with:  python -m app.main
(That's the container's default CMD.)
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

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


@asynccontextmanager
async def lifespan(app):
    init_db()
    log.info("Database ready.")

    start_listener()  # no-op if Telegram unconfigured

    interval = max(1, settings.poll_interval_minutes)
    scheduler.add_job(
        _safe_poll, "interval", minutes=interval, id="poll",
        next_run_time=None, max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        _safe_reminders, "interval", minutes=1, id="reminders",
        max_instances=1, coalesce=True,
    )
    scheduler.start()
    log.info("Scheduler started — polling every %d min.", interval)

    # Kick an immediate poll shortly after boot instead of waiting a full interval.
    scheduler.add_job(_safe_poll, "date", id="poll-now")

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
