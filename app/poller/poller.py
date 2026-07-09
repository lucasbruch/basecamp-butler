"""The polling job: fetch changed recordings, store raw events, checkpoint, classify."""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..basecamp.auth import get_token_row, get_valid_access_token
from ..basecamp.client import BasecampClient
from ..classifier import classify_new_events
from ..db import session_scope
from ..models import AppState, Checkpoint, Project, RawEvent
from ..util import parse_bc_datetime, utcnow

log = logging.getLogger(__name__)

# Basecamp recording types we care about, mapped to our internal event `type`.
RECORDING_TYPES = {
    "Todo": "todo",
    "Message": "message",
    "Comment": "comment",
}

PROJECTS_CACHE_TTL = timedelta(hours=24)


def _refresh_projects(db: Session, client: BasecampClient) -> None:
    """Refresh the cached project list at most once a day."""
    state = db.get(AppState, "projects_refreshed_at")
    if state and state.value:
        last = parse_bc_datetime(state.value)
        if last and utcnow() - last < PROJECTS_CACHE_TTL:
            return

    log.info("Refreshing project list from Basecamp.")
    seen: set[int] = set()
    for p in client.projects():
        seen.add(p["id"])
        row = db.get(Project, p["id"])
        if row is None:
            row = Project(id=p["id"], name=p.get("name", "?"))
            db.add(row)
        else:
            row.name = p.get("name", row.name)
    db.merge(AppState(key="projects_refreshed_at", value=utcnow().isoformat()))
    db.flush()
    log.info("Project cache holds %d projects.", len(seen))


def _capture_my_identity(db: Session, client: BasecampClient) -> None:
    """Store the authenticated user's id/name once — the classifier keys off it."""
    if db.get(AppState, "my_user_id"):
        return
    profile = client.my_profile()
    db.merge(AppState(key="my_user_id", value=str(profile.get("id"))))
    db.merge(AppState(key="my_name", value=profile.get("name", "")))
    log.info("Captured identity: %s (%s)", profile.get("name"), profile.get("id"))


def _enabled_bucket_ids(db: Session) -> list[int]:
    rows = db.execute(select(Project.id).where(Project.enabled.is_(True))).scalars()
    return list(rows)


def _poll_type(db: Session, client: BasecampClient, rec_type: str, event_type: str) -> int:
    """Fetch recordings newer than the checkpoint for one type; store raw events."""
    cp = db.get(Checkpoint, rec_type)
    if cp is None:
        cp = Checkpoint(resource_type=rec_type, last_seen_updated_at=None)
        db.add(cp)
        db.flush()
    watermark = cp.last_seen_updated_at

    # First ever poll for this type: don't backfill history (that would flood the
    # user with suggestions from old activity). Just seed the watermark to "now".
    if watermark is None:
        for item in client.recordings(rec_type):
            newest = parse_bc_datetime(item.get("updated_at"))
            if newest:
                cp.last_seen_updated_at = newest
            break  # recordings are newest-first, so the first item is the max
        db.flush()
        log.info("%s: seeded checkpoint (no backfill on first run).", rec_type)
        return 0

    new_count = 0
    highest = watermark
    for item in client.recordings(rec_type):
        updated = parse_bc_datetime(item.get("updated_at"))
        if updated is None:
            continue
        # Recordings come newest-first: once we reach the watermark we can stop.
        if watermark is not None and updated <= watermark:
            break
        if highest is None or updated > highest:
            highest = updated

        bucket = item.get("bucket") or {}
        stmt = (
            pg_insert(RawEvent)
            .values(
                project_id=bucket.get("id"),
                type=event_type,
                basecamp_id=item["id"],
                updated_at=updated,
                payload=item,
                processed=False,
            )
            .on_conflict_do_nothing(constraint="uq_raw_event")
        )
        db.execute(stmt)
        new_count += 1

    if highest is not None:
        cp.last_seen_updated_at = highest
    db.flush()
    if new_count:
        log.info("%s: %d new/updated recordings.", rec_type, new_count)
    return new_count


def run_poll_cycle() -> None:
    """One full poll: refresh token, walk each recording type, then classify."""
    with session_scope() as db:
        try:
            get_token_row(db)
        except RuntimeError:
            log.warning("No OAuth token yet — run scripts/authorize.py. Skipping poll.")
            return

        access = get_valid_access_token(db)
        token = get_token_row(db)
        if not token.account_id:
            log.warning("No account_id stored — re-run authorize.py. Skipping poll.")
            return

        client = BasecampClient(access, token.account_id, token.api_href)
        try:
            _capture_my_identity(db, client)
            _refresh_projects(db, client)
            total = 0
            for rec_type, event_type in RECORDING_TYPES.items():
                total += _poll_type(db, client, rec_type, event_type)
            for pid in _enabled_bucket_ids(db):
                proj = db.get(Project, pid)
                if proj:
                    proj.last_polled_at = utcnow()
        finally:
            client.close()

    log.info("Poll cycle stored %d new events; classifying…", total)
    classify_new_events()
