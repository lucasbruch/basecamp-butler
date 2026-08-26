"""The polling job: fetch changed recordings, store raw events, checkpoint, classify."""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .. import activity, autoreply, retention, runtime
from ..basecamp.auth import get_token_row, get_valid_access_token
from ..basecamp.client import BasecampClient
from ..classifier import classify_new_events
from ..config import settings
from ..db import session_scope
from ..models import RAW_EVENT_IDENTITY, AppState, Checkpoint, Project, RawEvent
from ..util import as_aware, parse_bc_datetime, safe_url, utcnow

_TAG_RE = re.compile(r"<[^>]+>")


def _plain(html: str | None, limit: int = 200) -> str:
    """Strip tags from a Basecamp HTML excerpt for readable log lines."""
    if not html:
        return ""
    return _TAG_RE.sub(" ", html).replace("&nbsp;", " ").strip()[:limit]


log = logging.getLogger(__name__)

# Basecamp recording types we care about, mapped to our internal event `type`.
# Schedule entries carry the meetings you're invited to — the single most
# action-bearing thing in Basecamp that this used to ignore entirely.
RECORDING_TYPES = {
    "Todo": "todo",
    "Message": "message",
    "Comment": "comment",
    "Schedule::Entry": "schedule",
    "Document": "document",
    "Upload": "upload",
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
            .on_conflict_do_nothing(index_elements=RAW_EVENT_IDENTITY)
        )
        # Count only rows that actually landed — a conflict (re-seen recording)
        # inserts nothing and shouldn't inflate the "N new items" heartbeat.
        if db.execute(stmt).rowcount:
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
            # Pass the watermark so the client stops paging as soon as it
            # reaches lines we already have (usually after page 1).
            lines, complete = client.chat_lines(bucket_id, chat_id, since_id=last_seen)
        except Exception:
            log.exception("Campfire %s: failed to fetch lines", chat_id)
            continue
        if not complete:
            # The client has already logged what was missed. A room is chatter
            # rather than correspondence, so this doesn't earn a feed entry.
            log.warning("Campfire %s: older lines were left unread.", chat_id)
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
            # Keep the room ids on the payload so the classifier can group a
            # room's lines into one conversation (same key pings use: _chat_id).
            payload = {**line, "_chat_id": chat_id, "_bucket_id": bucket_id}
            stmt = (
                pg_insert(RawEvent)
                .values(
                    project_id=bucket_id,
                    type="chat",
                    basecamp_id=lid,
                    updated_at=updated or utcnow(),
                    payload=payload,
                    processed=False,
                )
                .on_conflict_do_nothing(index_elements=RAW_EVENT_IDENTITY)
            )
            if db.execute(stmt).rowcount:
                new_count += 1
        db.merge(AppState(key=key, value=str(highest)))

    db.flush()
    if new_count:
        log.info("Campfire: %d new chat line(s).", new_count)
        activity.record(db, "campfire", f"{new_count} new Campfire chat line(s).")
    return new_count


_SUB_URL_RE = re.compile(r"/buckets/(\d+)/recordings/(\d+)")

# How deep to scan the notifications feed for ping threads. The feed is every
# kind of notification mixed together, so on a busy account a ping sent minutes
# ago can sit below a hundred project notifications sent seconds ago — three
# pages was a guess, and a wrong one. Ten is the hard cap; the quiet-page rule
# below usually stops long before it.
_PING_FEED_MAX_PAGES = 10
# Once pings have been found, this many consecutive ping-free pages means we've
# read past them into older news. Discovery is additive (one request per page,
# once per poll), not per-thread, so paging a little deeper is cheap.
_PING_FEED_QUIET_PAGES = 2

# How far back to read when a conversation is met for the first time (after the
# app's own first run). Long enough that a ping sent overnight is still picked
# up in the morning, short enough that a dormant thread resurfacing in the feed
# can't turn its history into a pile of to-dos.
NEW_THREAD_LOOKBACK_HOURS = 24


def _fetch_ping_notifications(client: BasecampClient) -> list[dict]:
    """Return ping entries from the notifications feed — used only to *discover*
    which Circle conversations are active. The feed carries one entry per
    conversation with a single preview line, so we don't ingest from it directly;
    we read each thread's real messages via the chat-lines endpoint.

    Pings bubble up with everything else, so "how deep" can't be a flat number:
    we keep reading until the pings run out (`_PING_FEED_QUIET_PAGES` in a row
    with none), or the feed does, or the hard cap does. A page with no pings
    *before* we've found any is not a reason to stop — that's exactly the busy
    account where the old three-page scan lost threads.
    """
    collected: list[dict] = []
    quiet = 0
    for page in range(1, _PING_FEED_MAX_PAGES + 1):
        feed = client.my_readings(page=page)
        notifications = (feed.get("unreads") or []) + (feed.get("reads") or [])
        if not notifications:
            break
        found = [n for n in notifications if n.get("section") == "pings"]
        collected.extend(found)
        if found:
            quiet = 0
        elif collected:
            quiet += 1
            if quiet >= _PING_FEED_QUIET_PAGES:
                break
    return collected


def _ping_conversations(notifications: list[dict]) -> dict:
    """Map ping notifications to unique (circle_id, chat_id) threads, keeping the
    latest notification per thread (for its app_url deep link).

    `notifications` arrives newest-first (page 1 leads), so the *first* entry
    seen for a thread is the freshest one — assigning would have kept the last,
    i.e. the stalest deep link we could find.
    """
    convos: dict[tuple[int, int], dict] = {}
    skipped = 0
    for n in notifications:
        m = _SUB_URL_RE.search(n.get("subscription_url") or "")
        if m:
            convos.setdefault((int(m.group(1)), int(m.group(2))), n)
        else:
            skipped += 1
    if skipped:
        # Silence here used to be indistinguishable from "nobody pinged you".
        # If Basecamp ever changes the shape of this field, this line is the
        # only thing that will say so.
        log.warning(
            "Pings: %d feed entr(ies) had no readable /buckets/<id>/recordings/<id> "
            "link and were skipped — their conversations can't be found this way.",
            skipped,
        )
    return convos


# Threads we have ingested before, so they can be polled directly even when the
# notifications feed has stopped mentioning them.
KNOWN_THREAD_PREFIX = "ping_thread_"
# A conversation quiet for three weeks is still one you'd want answered when it
# wakes up, and remembering it costs nothing until it does.
KNOWN_THREAD_DAYS = 30
# This one is not free: every remembered thread is one chat-lines request per
# poll. Against a 50-requests-per-10s limit shared with everything else the
# poller does, 25 is the ceiling — the most recently active win, and anything
# past it is still reachable through the feed scan above.
MAX_KNOWN_THREADS = 25


def _remember_ping_thread(
    db: Session, circle_id: int, chat_id: int, app_url: str | None
) -> None:
    """Record that this conversation exists and has just had something in it."""
    key = f"{KNOWN_THREAD_PREFIX}{chat_id}"
    row = db.get(AppState, key)
    previous: dict = {}
    if row and row.value:
        try:
            loaded = json.loads(row.value)
        except ValueError:
            loaded = None
        if isinstance(loaded, dict):
            previous = loaded
    db.merge(AppState(key=key, value=json.dumps({
        "circle": circle_id,
        "seen": utcnow().isoformat(),
        # Hold on to the last deep link we were given: a thread reached without
        # a notification has none, and blanking it would strip the "open in
        # Basecamp" links off everything ingested that way.
        "url": app_url or previous.get("url"),
    })))
    # Flush so a second call in the same transaction reads back what this one
    # wrote — a pending merge isn't in the identity map, so `db.get` above would
    # miss it and we'd queue two inserts for the same key.
    db.flush()


def _known_ping_threads(db: Session) -> dict:
    """(circle_id, chat_id) -> a notification-shaped dict, for threads we know.

    The notifications feed is the only way to *discover* a Ping conversation,
    and it is a firehose: on a busy account a live thread can be pushed past the
    few pages we read, at which point its new messages stop being ingested at
    all and the butler goes quiet on it for no visible reason. Once a thread is
    known we poll it directly for a week, feed or no feed.
    """
    cutoff = utcnow() - timedelta(days=KNOWN_THREAD_DAYS)
    found: list[tuple] = []
    rows = db.execute(
        select(AppState).where(AppState.key.like(f"{KNOWN_THREAD_PREFIX}%"))
    ).scalars()
    for row in rows:
        chat = row.key[len(KNOWN_THREAD_PREFIX):]
        if not chat.isdigit():
            continue
        try:
            rec = json.loads(row.value or "{}")
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        circle = rec.get("circle")
        try:
            seen = parse_bc_datetime(rec.get("seen"))
        except (TypeError, ValueError):
            seen = None
        if not isinstance(circle, int) or seen is None or seen < cutoff:
            continue
        found.append((seen, (circle, int(chat)), {"app_url": rec.get("url")}))
    found.sort(key=lambda item: item[0], reverse=True)
    return {key: notif for _, key, notif in found[:MAX_KNOWN_THREADS]}


def _ingest_ping_chat(
    db: Session,
    client: BasecampClient,
    circle_id: int,
    chat_id: int,
    notif: dict,
    *,
    first_run: bool = False,
) -> int:
    """Read one Ping conversation's actual messages via the chat-lines endpoint
    and store each new line as a `ping` event.

    Watermark per thread by the highest line id we've seen (same scheme as
    Campfire).

    A thread with no watermark is one of two very different things, and treating
    them alike is how first messages went missing. On the app's **first run**
    (`first_run`) every conversation is unseen, and reading them in would turn
    years of history into to-dos — so those only seed a watermark. Afterwards, a
    thread without a watermark is one that has just *started*: its opening line
    is the whole point, and dropping it meant a new person's first ping — and
    with the auto-reply watermark seeding on top, their second — got silence.
    Those are ingested, bounded by `NEW_THREAD_LOOKBACK_HOURS` so a long-quiet
    conversation resurfacing in the feed still can't dump a month of backlog.
    """
    key = f"ping_cp_{chat_id}"
    state = db.get(AppState, key)
    last_seen = int(state.value) if state and (state.value or "").isdigit() else None

    try:
        lines, complete = client.chat_lines(circle_id, chat_id, since_id=last_seen)
    except Exception:
        log.exception("Ping thread %s: failed to fetch lines", chat_id)
        return 0
    if not isinstance(lines, list) or not lines:
        return 0
    if not complete:
        # The watermark below jumps to the newest line regardless, so this is the
        # one moment the gap can be named. Say it out loud rather than let the
        # butler look like it simply had nothing to say about those messages.
        activity.record(
            db,
            "error",
            "A Ping conversation had more unread history than one poll can "
            "read. The most recent messages were taken; older ones were "
            "skipped and won't be revisited.",
            detail=f"circle={circle_id} chat={chat_id}",
            url=safe_url((notif or {}).get("app_url")),
        )

    app_url = safe_url((notif or {}).get("app_url"))
    if last_seen is None and first_run:
        seed = max((ln.get("id", 0) for ln in lines), default=0)
        db.merge(AppState(key=key, value=str(seed)))
        _remember_ping_thread(db, circle_id, chat_id, app_url)
        return 0  # no backfill of history the very first time we look

    # A brand-new thread has nothing below it, so "newer than the watermark"
    # becomes "recent enough to still matter".
    cutoff = (
        utcnow() - timedelta(hours=NEW_THREAD_LOOKBACK_HOURS)
        if last_seen is None
        else None
    )

    highest = last_seen or 0
    count = 0
    skipped_old = 0
    for line in lines:
        lid = line.get("id", 0)
        if last_seen is not None and lid <= last_seen:
            continue
        created = parse_bc_datetime(line.get("created_at") or line.get("updated_at"))
        if cutoff is not None and as_aware(created or utcnow()) < cutoff:
            # Still moves the watermark past it: this line is history, and we
            # don't want to reconsider it on every poll from here on.
            highest = max(highest, lid)
            skipped_old += 1
            continue
        highest = max(highest, lid)
        # Keep the deep link + circle/chat ids on the payload for the classifier/UI.
        payload = {**line, "_circle_id": circle_id, "_chat_id": chat_id, "app_url": app_url}
        stmt = (
            pg_insert(RawEvent)
            .values(
                project_id=None,  # Circles aren't projects
                type="ping",
                basecamp_id=lid,
                updated_at=created or utcnow(),
                payload=payload,
                processed=False,
            )
            .on_conflict_do_nothing(index_elements=RAW_EVENT_IDENTITY)
        )
        if not db.execute(stmt).rowcount:
            continue  # already ingested (re-seen line) — don't double-count/log
        count += 1

        sender = (line.get("creator") or {}).get("name") or "someone"
        excerpt = _plain(line.get("content"))
        activity.record(
            db,
            "ping",
            f"New Ping from {sender}"
            + (f": “{excerpt}”" if excerpt else " (no preview text)."),
            detail=f"circle={circle_id} chat={chat_id} line={lid}",
            url=app_url,
        )
    db.merge(AppState(key=key, value=str(highest)))
    if skipped_old:
        log.info(
            "Ping thread %s is new to us: took %d recent line(s), left %d older "
            "than %dh alone.",
            chat_id,
            count,
            skipped_old,
            NEW_THREAD_LOOKBACK_HOURS,
        )
    # `last_seen is None` also counts: a thread met for the first time has to be
    # remembered even when every line in it was too old to take, or it drops off
    # the known-threads list and only the feed can ever find it again.
    if count or last_seen is None:
        _remember_ping_thread(db, circle_id, chat_id, app_url)
    return count


def _poll_pings(db: Session, client: BasecampClient) -> int:
    """Ingest Ping (direct-message) *messages*.

    Pings aren't in projects/recordings.json — they live in `Circle` buckets. The
    notifications feed (/my/readings.json) only carries one preview entry per
    conversation, so we use it purely to discover active ping threads, then read
    each thread's real messages via the chat-lines endpoint (same as Campfire).
    That's the only way to catch every message instead of a single stale preview.
    """
    try:
        notifications = _fetch_ping_notifications(client)
    except Exception:
        log.exception("Failed to fetch notifications feed for pings")
        activity.record(
            db, "error", "Could not read the Pings (direct-message) feed from Basecamp."
        )
        return 0

    convos = _ping_conversations(notifications)
    # Threads the feed no longer mentions, but which were active this week, get
    # polled anyway. `setdefault` so a feed entry always wins — its deep link is
    # the fresher one.
    for known, notif in _known_ping_threads(db).items():
        convos.setdefault(known, notif)
    first_run = db.get(AppState, "pings_seeded") is None

    new_count = 0
    for (circle_id, chat_id), notif in convos.items():
        new_count += _ingest_ping_chat(
            db, client, circle_id, chat_id, notif, first_run=first_run
        )

    if first_run:
        db.merge(AppState(key="pings_seeded", value=utcnow().isoformat()))
        log.info("Pings: seeded %d thread(s) (no backfill on first run).", len(convos))
        activity.record(
            db,
            "ping",
            f"First look at Pings — found {len(convos)} active conversation(s), "
            "starting fresh (existing messages won't be turned into to-dos).",
        )

    # Heartbeat for the dashboard.
    db.merge(AppState(key="pings_checked_at", value=utcnow().isoformat()))
    db.merge(AppState(key="pings_visible", value=str(len(convos))))
    db.flush()
    if new_count:
        log.info("Pings: %d new direct message(s).", new_count)
    return new_count


def run_poll_cycle() -> None:
    """One full poll (with heartbeat), then classify whatever it stored.

    The poll and the classifier run as separate steps — and the classifier also
    runs on its own schedule (see main.py) — so a backlog left behind by an
    unreachable LLM drains as soon as the LLM is back, without waiting for or
    depending on a successful poll.
    """
    failure: Exception | None = None
    try:
        total = _poll_basecamp()
    except Exception as exc:
        # Never let a poll failure vanish into stdout: record it so the dashboard
        # and /activity page show a *broken* poll instead of a frozen clock.
        _record_poll_failure(exc)
        failure = exc
    else:
        log.info("Poll cycle stored %d new events; classifying…", total)

    # Both of these run even when the fetch above failed. Whatever earlier
    # cycles stored is still sitting there unprocessed and unanswered, and one
    # bad token refresh used to mean nobody got a reply that cycle at all.
    #
    # Guarded for the same reason the reply pass below is: an exception here
    # would skip the replies entirely *and* swallow the poll failure recorded
    # above, so the one step that was still meant to run wouldn't.
    try:
        classify_new_events()
    except Exception:
        log.exception("Classification failed")

    # Replies come last, and separately: they read the ingested ping lines
    # directly rather than the classifier's output, so a classifier that is off,
    # rule-based, or backed up doesn't change what gets answered. A no-op unless
    # auto-reply is switched on and somebody is on the allowlist.
    try:
        autoreply.run_pass()
    except Exception:
        log.exception("Auto-reply pass failed")

    if failure is not None:
        raise failure


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

            # Heartbeat for the dashboard and the Settings project list; only add
            # a feed row when there's news, so idle cycles don't bury the
            # interesting entries.
            db.merge(AppState(key="last_poll_at", value=utcnow().isoformat()))
            db.merge(AppState(key="last_poll_new", value=str(total)))
            db.merge(AppState(key="last_poll_ok", value="1"))
            db.merge(AppState(key="last_poll_error", value=""))
            if total:
                activity.record(
                    db, "poll", f"Checked Basecamp — {total} new item(s) to look at."
                )
            activity.prune(db)
            retention.sweep(db, runtime.load(db))
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
