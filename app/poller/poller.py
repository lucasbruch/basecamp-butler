"""The polling job: fetch changed recordings, store raw events, checkpoint, classify."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import activity
from ..basecamp.auth import get_token_row, get_valid_access_token
from ..basecamp.client import BasecampClient
from ..classifier import classify_new_events
from ..config import settings
from ..db import session_scope
from ..models import AppState, Checkpoint, Project, RawEvent
from ..util import parse_bc_datetime, utcnow

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html: str | None, limit: int = 200) -> str:
    """Strip tags from a Basecamp HTML excerpt for readable log lines."""
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()[:limit]


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


def _poll_campfires(db: Session, client: BasecampClient) -> int:
    """Poll Campfire chat lines. Checkpoints per room (by max line id) in app_state.

    Campfire has no recordings-endpoint support and no 'updated since' filter, so
    we track the highest line id we've seen per room. First sight of a room only
    seeds the watermark (no backfill of chat history).
    """
    new_count = 0
    for cf in client.campfires():
        bucket = cf.get("bucket") or {}
        bucket_id, chat_id = bucket.get("id"), cf.get("id")
        if not bucket_id or not chat_id:
            continue

        key = f"chat_cp_{chat_id}"
        state = db.get(AppState, key)
        last_seen = int(state.value) if state and (state.value or "").isdigit() else None

        try:
            lines = client.chat_lines(bucket_id, chat_id)
        except Exception:
            log.exception("Campfire %s: failed to fetch lines", chat_id)
            continue
        if not isinstance(lines, list) or not lines:
            continue

        if last_seen is None:
            seed = max((ln.get("id", 0) for ln in lines), default=0)
            db.merge(AppState(key=key, value=str(seed)))
            continue

        highest = last_seen
        for line in lines:
            lid = line.get("id", 0)
            if lid <= last_seen:
                continue
            highest = max(highest, lid)
            updated = parse_bc_datetime(line.get("updated_at") or line.get("created_at"))
            stmt = (
                pg_insert(RawEvent)
                .values(
                    project_id=bucket_id,
                    type="chat",
                    basecamp_id=lid,
                    updated_at=updated or utcnow(),
                    payload=line,
                    processed=False,
                )
                .on_conflict_do_nothing(constraint="uq_raw_event")
            )
            db.execute(stmt)
            new_count += 1
        db.merge(AppState(key=key, value=str(highest)))

    db.flush()
    if new_count:
        log.info("Campfire: %d new chat line(s).", new_count)
        activity.record(db, "campfire", f"{new_count} new Campfire chat line(s).")
    return new_count


_PING_CHECKPOINT = "Ping"
_SUB_URL_RE = re.compile(r"/buckets/(\d+)/recordings/(\d+)")


def _poll_pings(db: Session, client: BasecampClient) -> int:
    """Ingest Pings (direct messages) from the account notifications feed.

    Pings aren't in projects/recordings.json — they live in `Circle` buckets and
    only surface via /my/readings.json with section == "pings". We treat each new
    ping notification as an event (using its content excerpt + sender).
    """
    cp = db.get(Checkpoint, _PING_CHECKPOINT)
    if cp is None:
        cp = Checkpoint(resource_type=_PING_CHECKPOINT, last_seen_updated_at=None)
        db.add(cp)
        db.flush()
    watermark = cp.last_seen_updated_at

    try:
        feed = client.my_readings(page=1)
    except Exception:
        log.exception("Failed to fetch notifications feed for pings")
        activity.record(
            db, "error", "Could not read the Pings (direct-message) feed from Basecamp."
        )
        return 0

    # Newest activity first across unreads + reads.
    notifications = (feed.get("unreads") or []) + (feed.get("reads") or [])
    pings = [n for n in notifications if (n.get("section") == "pings")]

    if watermark is None:
        newest = max(
            (parse_bc_datetime(n.get("created_at")) for n in pings if n.get("created_at")),
            default=None,
        )
        cp.last_seen_updated_at = newest
        db.flush()
        log.info("Pings: seeded checkpoint (no backfill on first run).")
        activity.record(
            db,
            "ping",
            f"First look at Pings — saw {len(pings)} in the feed, starting fresh "
            "(existing messages won't be turned into to-dos).",
        )
        return 0

    new_count = 0
    highest = watermark
    for n in pings:
        created = parse_bc_datetime(n.get("created_at"))
        if created is None or created <= watermark:
            continue
        if created > highest:
            highest = created

        circle_id = chat_id = None
        m = _SUB_URL_RE.search(n.get("subscription_url") or "")
        if m:
            circle_id, chat_id = int(m.group(1)), int(m.group(2))
        # Enrich payload with parsed ids so the classifier/UI can deep-link.
        payload = {**n, "_circle_id": circle_id, "_chat_id": chat_id}

        stmt = (
            pg_insert(RawEvent)
            .values(
                project_id=None,  # Circles aren't projects
                type="ping",
                basecamp_id=n["id"],
                updated_at=created,
                payload=payload,
                processed=False,
            )
            .on_conflict_do_nothing(constraint="uq_raw_event")
        )
        db.execute(stmt)
        new_count += 1

        sender = (n.get("creator") or {}).get("name") or "someone"
        # Ping text lives in `content_excerpt` (see the pings-API notes).
        excerpt = _plain(n.get("content_excerpt") or n.get("content"))
        activity.record(
            db,
            "ping",
            f"New Ping from {sender}"
            + (f": “{excerpt}”" if excerpt else " (no preview text)."),
            detail=f"created_at={n.get('created_at')}\ncircle={circle_id} chat={chat_id}",
            url=n.get("app_url"),
        )

    cp.last_seen_updated_at = highest
    # Heartbeat: record that we looked (shown on the dashboard) instead of
    # spamming the feed with a "nothing new" row every single poll.
    db.merge(AppState(key="pings_checked_at", value=utcnow().isoformat()))
    db.merge(AppState(key="pings_visible", value=str(len(pings))))
    db.flush()
    if new_count:
        log.info("Pings: %d new direct-message notification(s).", new_count)
    return new_count


def run_poll_cycle() -> None:
    """One full poll (with heartbeat), then classify whatever it stored.

    The poll and the classifier run as separate steps — and the classifier also
    runs on its own schedule (see main.py) — so a backlog left behind by an
    unreachable LLM drains as soon as the LLM is back, without waiting for or
    depending on a successful poll.
    """
    try:
        total = _poll_basecamp()
    except Exception as exc:
        # Never let a poll failure vanish into stdout: record it so the dashboard
        # and /activity page show a *broken* poll instead of a frozen clock.
        _record_poll_failure(exc)
        raise

    log.info("Poll cycle stored %d new events; classifying…", total)
    classify_new_events()


def _poll_basecamp() -> int:
    """Fetch changed recordings and store raw events; return the new-event count.

    Writes a success heartbeat into app_state. Raises on any hard failure (token
    refresh, transport, DB) — the caller turns that into a failure heartbeat.
    """
    with session_scope() as db:
        try:
            get_token_row(db)
        except RuntimeError:
            log.warning("No OAuth token yet — run scripts/authorize.py. Skipping poll.")
            return 0

        access = get_valid_access_token(db)
        token = get_token_row(db)
        if not token.account_id:
            log.warning("No account_id stored — re-run authorize.py. Skipping poll.")
            return 0

        client = BasecampClient(access, token.account_id, token.api_href)
        try:
            _capture_my_identity(db, client)
            _refresh_projects(db, client)
            total = 0
            for rec_type, event_type in RECORDING_TYPES.items():
                total += _poll_type(db, client, rec_type, event_type)
            if settings.poll_campfire:
                total += _poll_campfires(db, client)
            if settings.poll_pings:
                total += _poll_pings(db, client)
            for pid in _enabled_bucket_ids(db):
                proj = db.get(Project, pid)
                if proj:
                    proj.last_polled_at = utcnow()

            # Heartbeat for the dashboard; only add a feed row when there's news,
            # so idle cycles don't bury the interesting entries.
            db.merge(AppState(key="last_poll_at", value=utcnow().isoformat()))
            db.merge(AppState(key="last_poll_new", value=str(total)))
            db.merge(AppState(key="last_poll_ok", value="1"))
            db.merge(AppState(key="last_poll_error", value=""))
            if total:
                activity.record(
                    db, "poll", f"Checked Basecamp — {total} new item(s) to look at."
                )
            activity.prune(db)
        finally:
            client.close()

    return total


def _record_poll_failure(exc: Exception) -> None:
    """Persist a failed-poll heartbeat + activity row in a fresh transaction.

    _poll_basecamp's own session has already rolled back by the time we get here,
    so we open a new one solely to record the failure. Best-effort: if even this
    write fails, log and move on — the next cycle will try again.
    """
    msg = f"{type(exc).__name__}: {exc}".strip()[:500]
    log.warning("Poll failed: %s", msg)
    try:
        with session_scope() as db:
            db.merge(AppState(key="last_poll_at", value=utcnow().isoformat()))
            db.merge(AppState(key="last_poll_ok", value="0"))
            db.merge(AppState(key="last_poll_error", value=msg))
            activity.record(
                db, "error", f"Poll failed — {msg}. Will retry next cycle."
            )
    except Exception:
        log.exception("Could not record the poll-failure heartbeat")
