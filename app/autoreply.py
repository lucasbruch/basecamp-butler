"""Answer Basecamp Pings on your behalf — carefully.

Everything else in this app writes into *your* copy of the world: a suggestion
on the dashboard, a to-do in a list you nominated. This module is the first one
that puts words in your mouth in front of somebody else, so it is built to be
hard to fire by accident and easy to audit afterwards.

The constraints, in the order they're enforced:

  * **Off unless asked.** `autoreply_enabled` is off by default.
  * **Allowlist only.** A Ping is only ever answered if an `AutoReplyRule` names
    its sender. No rule, no reply — there is no "reply to everyone" setting.
  * **Pings only.** Campfire (group chat) and project messages are never
    answered; a room full of people is not a conversation you can safely
    autopilot.
  * **One named conversation per rule.** A Ping is not always a direct message —
    it can have several people in it — so a rule also names the `chat_id` it may
    speak in, and speaks nowhere else. This is deliberately not inferred: a group
    where only one person has spoken looks exactly like a direct message, so
    working it out from the transcript would be a guess, and the cost of guessing
    wrong is a message in your name in front of people you didn't mean. A rule
    with no conversation named answers nothing.
  * **Draft by default.** A rule's mode decides what happens to the drafted
    text: ``draft`` parks it on /replies for you to read and send, ``auto``
    posts it straight away. New rules are created as drafts.
  * **No backlog.** First sight of a thread only records a watermark, so
    switching the feature on never fires a volley of replies at old messages.
  * **Bounded.** One reply per thread per cooldown window, a hard daily ceiling
    across all threads, and nothing auto-sent during quiet hours.
  * **Never the last word.** If the newest line in the thread is already ours,
    there's nothing to answer.
  * **Never twice.** Wording that is already in a thread is never posted into
    it again, however the pass arrived at it.
  * **Nothing stale.** A line nobody answered within `STALE_AFTER_HOURS` is
    left alone — turning up half a day late in someone's inbox, in your name,
    is worse than staying quiet.

Every composed reply — sent, drafted, discarded or failed — is stored in
`autoreplies`, so "what has it said in my name?" has one answer.

## The watermark, and what may move it

`autoreply_cp_<chat>` is the highest `raw_events.id` this module has **finished
with** for that conversation. Moving it past a line means that line will never
be looked at again, so only a *final* decision may move it: nobody to answer,
nobody on the list, a model that read the exchange and judged it needed no
reply, or a reply actually composed.

A **transient** hold must not move it. This is where replies used to go to die:
the cooldown branch consumed the very messages it was declining to answer yet,
so a follow-up sent five minutes after a reply was dropped for good rather than
answered once the window passed — which reads, from the outside, as "it replied
once and then stopped". Holds now simply return, leaving the lines in place for
the next pass to reconsider.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import activity, runtime
from .basecamp.client import client_for
from .classifier import conversation
from .classifier.rules import _my_user_id
from .config import settings
from .db import session_scope
from .models import AppState, AutoReply, AutoReplyRule, RawEvent
from .runtime import RuntimeConfig
from .util import as_aware, as_html, parse_bc_datetime, safe_url, utcnow

log = logging.getLogger(__name__)

# Per-thread high-water mark — see the module docstring. Same scheme the poller
# uses for ingestion, kept separate so the two can't interfere.
CP_PREFIX = "autoreply_cp_"

MODES = ("draft", "auto")

# Reply rows that still need you. A `failed` row is an unsent reply whose
# delivery didn't land — as actionable as a draft, and listed with them.
PENDING_STATUSES = ("draft", "failed")

# How many earlier lines of the thread to show the model for context.
CONTEXT_LINES = 8

# Two separate budgets. Examining a thread is a couple of indexed queries, so a
# generous cap there costs little and is what stops a busy account's quieter
# conversations from starving. *Drafting* is a blocking LLM round trip (up to
# 120s), so that gets the tight budget — and a thread that runs out of budget
# keeps its watermark, which is what makes "next pass" true rather than a hope.
MAX_THREADS_PER_PASS = 50
MAX_DRAFTS_PER_PASS = 5

# Ceiling on how many ping rows one pass pulls back. Newest-first, because the
# newest lines are the only ones that could still need answering.
MAX_EVENTS_PER_PASS = 500

# How far back a pass looks for ping lines. Bounds the query against the
# fastest-growing table in the schema.
LOOKBACK_HOURS = 24

# A line older than this is never answered. Cooldowns, quiet hours and an LLM
# host that was asleep all defer a reply rather than dropping it, so something
# has to say when deferring has gone on too long to be worth it — long enough to
# ride out a NAS reboot, short enough that nobody gets an answer to yesterday
# morning's message.
STALE_AFTER_HOURS = 6

# How far back the duplicate check looks for wording we have already used in a
# thread. Longer than the cooldown on purpose: the cooldown is about how often
# it is polite to speak, this is about never saying the same thing twice.
DUPLICATE_WINDOW_HOURS = 24

# How far back `known_conversations` looks when building the list of Pings a rule
# can be pointed at. Wider than a reply pass on purpose — you pin a conversation
# once, and the one you want may have been quiet for a fortnight.
CONVERSATION_SCAN_DAYS = 30
CONVERSATION_SCAN_LINES = 2000

# Somebody pinging you who isn't on the allowlist is the normal case and must
# not fill the activity feed — but staying *completely* silent about it is why a
# mistyped name looks exactly like a broken feature. One note per window.
UNLISTED_NOTICE_HOURS = 6
UNLISTED_NOTICE_KEY = "autoreply_unlisted_at"

# Where the last decision about each conversation is kept, and the last summary
# of the pass as a whole. Not an event log: one row per thread, overwritten every
# time, so /replies can answer "why is it quiet?" without anyone reading
# container logs and without a per-minute pass burying the activity feed.
WHY_PREFIX = "autoreply_why_"
LAST_PASS_KEY = "autoreply_last_pass"
WHY_KEEP_DAYS = 7

# Set on an `auto` reply that quiet hours held back. Matched by prefix when the
# window ends, so rows written by older versions are released too.
HELD_QUIET = "quiet hours — held until they're over"

# Only one pass at a time — both the poll cycle and a one-minute job trigger it,
# and a slow LLM must not let two passes read the same watermark and answer the
# same thread twice.
_lock = threading.Lock()


# ── helpers ─────────────────────────────────────────────────────────────────
def enabled_rules(db: Session) -> dict[str, AutoReplyRule]:
    """Enabled rules keyed by lower-cased name, as senders are matched."""
    rows = db.execute(
        select(AutoReplyRule).where(AutoReplyRule.enabled.is_(True))
    ).scalars()
    return {(r.name or "").strip().lower(): r for r in rows if (r.name or "").strip()}


def _sender(event: RawEvent) -> str:
    # Bounded to the width of `AutoReply.person` / `AutoReplyRule.name`: this is
    # a Basecamp display name going straight into a String(200), and an INSERT
    # that overflows would take down the whole pass, not just this reply.
    name = ((event.payload or {}).get("creator") or {}).get("name", "") or ""
    return name[:200]


def owner_name(db: Session) -> str:
    """The account owner's display name — the model writes as this person."""
    row = db.get(AppState, "my_name")
    return ((row.value if row else "") or "").strip() or "the account owner"


def _watermark(db: Session, chat_id) -> int | None:
    row = db.get(AppState, f"{CP_PREFIX}{chat_id}")
    if row and (row.value or "").isdigit():
        return int(row.value)
    return None


def _set_watermark(db: Session, chat_id, value: int) -> None:
    db.merge(AppState(key=f"{CP_PREFIX}{chat_id}", value=str(value)))


def _normalise(text: str) -> str:
    """Collapse a reply to what a reader would call "the same message"."""
    return " ".join((text or "").split()).lower()


def already_said(
    db: Session,
    chat_id,
    text: str,
    *,
    statuses: tuple[str, ...] = ("draft", "sent"),
    exclude_id: int | None = None,
) -> bool:
    """True if this exact wording is already in the thread's recent history.

    A model shown a transcript that ends in "haha" will happily hand back the
    line it wrote an hour ago, word for word — its own previous reply is sitting
    right there in the context it was given. Repeating yourself is the one
    mistake here the other person sees twice, so wording that has already gone
    out (or is already waiting as a draft) never goes out again.
    """
    wanted = _normalise(text)
    if not wanted or chat_id is None:
        return False
    since = utcnow() - timedelta(hours=DUPLICATE_WINDOW_HOURS)
    stmt = select(AutoReply.draft).where(
        AutoReply.chat_id == chat_id,
        AutoReply.status.in_(statuses),
        AutoReply.created_at >= since,
    )
    if exclude_id is not None:
        stmt = stmt.where(AutoReply.id != exclude_id)
    return any(_normalise(draft) == wanted for (draft,) in db.execute(stmt))


def sent_today(db: Session) -> int:
    """Replies actually delivered in the last 24 hours, for the daily ceiling."""
    since = utcnow() - timedelta(hours=24)
    return int(
        db.execute(
            select(func.count(AutoReply.id)).where(
                AutoReply.status == "sent", AutoReply.sent_at >= since
            )
        ).scalar()
        or 0
    )


def pending_draft(db: Session, chat_id) -> bool:
    """True while an unsent reply for this thread is waiting on /replies.

    The conversation is already waiting on you, and stacking a second draft on
    top of the first helps nobody. Unlike the behaviour this replaces, it only
    *defers*: the newer lines keep their place and are answered once you send or
    discard what's already there.

    `failed` counts as waiting for the same reason `draft` does — the reply was
    written, nothing reached Basecamp, and it's sitting on the page with a Send
    button under it.
    """
    if chat_id is None:
        return False
    row = db.execute(
        select(AutoReply.id)
        .where(
            AutoReply.chat_id == chat_id,
            AutoReply.status.in_(PENDING_STATUSES),
        )
        .limit(1)
    ).first()
    return row is not None


def replied_recently(db: Session, chat_id, cooldown_minutes: int) -> bool:
    """True if we actually *said* something in this thread inside the window.

    Counts sends only. A draft doesn't speak, so it doesn't start the "how often
    is it polite to speak" clock — `pending_draft` covers that case with its own
    (and stricter) rule.
    """
    if cooldown_minutes <= 0 or chat_id is None:
        return False
    since = utcnow() - timedelta(minutes=cooldown_minutes)
    row = db.execute(
        select(AutoReply.id)
        .where(
            AutoReply.chat_id == chat_id,
            AutoReply.status == "sent",
            AutoReply.sent_at >= since,
        )
        .limit(1)
    ).first()
    return row is not None


def _too_old(event: RawEvent) -> bool:
    """True if this line is past the point where answering it helps."""
    when = as_aware(getattr(event, "updated_at", None))
    if when is None:
        return False
    return utcnow() - when > timedelta(hours=STALE_AFTER_HOURS)


def _note_unlisted(db: Session, names: list[str]) -> None:
    """Say, once in a while, who pinged that nobody had put on the list.

    A rule matches the Basecamp display name exactly (case aside), so "Ana" set
    against an account that shows "Ana Müller" answers nobody. Without this,
    that mistake and a broken feature look identical from the UI.
    """
    unique = sorted({n.strip() for n in names if n and n.strip()})
    if not unique:
        return
    row = db.get(AppState, UNLISTED_NOTICE_KEY)
    last = _parse_stamp(row.value if row else None)
    if last and utcnow() - last < timedelta(hours=UNLISTED_NOTICE_HOURS):
        return
    activity.record(
        db,
        "reply",
        "Pinged by "
        + ", ".join(unique)
        + " — nobody there is on the auto-reply list, so nothing was said. A "
        "rule's name has to match Basecamp's spelling exactly.",
    )
    db.merge(AppState(key=UNLISTED_NOTICE_KEY, value=utcnow().isoformat()))


def _note(db: Session, chat_id, person: str, why: str) -> None:
    """Remember what was decided about one conversation, replacing what was.

    Flushed rather than left pending: several of these land in one pass, and a
    pending merge isn't in the identity map — the next `db.get` for the same key
    would miss it and queue a second insert.
    """
    db.merge(AppState(key=f"{WHY_PREFIX}{chat_id}", value=json.dumps({
        "person": (person or "").strip(),
        "why": why,
        "at": utcnow().isoformat(),
    })))
    db.flush()


def _note_pass(db: Session, why: str, threads: int = 0, composed: int = 0) -> None:
    """Remember how the pass as a whole went.

    Flushed for the same reason, and because most of the passes worth explaining
    are the ones that return early without reaching the flush at the end.
    """
    db.merge(AppState(key=LAST_PASS_KEY, value=json.dumps({
        "why": why,
        "threads": threads,
        "composed": composed,
        "at": utcnow().isoformat(),
    })))
    db.flush()


def decisions(db: Session) -> list[dict]:
    """Every conversation's last decision, newest first — for the /replies page.

    Also drops entries for conversations nothing has been decided about in a
    week, so the table doesn't accumulate threads that ended months ago.
    """
    out: list[dict] = []
    stale = utcnow() - timedelta(days=WHY_KEEP_DAYS)
    rows = db.execute(
        select(AppState).where(AppState.key.like(f"{WHY_PREFIX}%"))
    ).scalars().all()
    for row in rows:
        try:
            rec = json.loads(row.value or "{}")
        except ValueError:
            rec = None
        when = _parse_stamp((rec or {}).get("at"))
        if not isinstance(rec, dict) or when is None:
            db.delete(row)
            continue
        if when < stale:
            db.delete(row)
            continue
        out.append({
            "chat_id": row.key[len(WHY_PREFIX):],
            "person": rec.get("person") or "someone",
            "why": rec.get("why") or "",
            "at": when,
        })
    out.sort(key=lambda d: d["at"], reverse=True)
    return out


def recent_senders(db: Session) -> list[str]:
    """Every name that has pinged you lately, exactly as Basecamp spells it.

    The single most useful fact for "why didn't it reply?", because a rule is
    matched against this string and nothing in the UI ever showed it. "Ana" set
    against an account that displays "Ana Müller" answers nobody, and looks
    identical to a feature that doesn't work.
    """
    my_id = _my_user_id(db)
    rows = (
        db.execute(
            select(RawEvent)
            .where(
                RawEvent.type == "ping",
                RawEvent.updated_at >= utcnow() - timedelta(hours=LOOKBACK_HOURS),
            )
            .order_by(RawEvent.id.desc())
            .limit(MAX_EVENTS_PER_PASS)
        )
        .scalars()
        .all()
    )
    names: list[str] = []
    for event in rows:
        if conversation.is_own(event, my_id):
            continue
        name = _sender(event).strip()
        if name and name not in names:
            names.append(name)
    return names


def known_conversations(db: Session) -> list[dict]:
    """Every Ping conversation the butler has seen, newest first — for the picker.

    A rule has to name the conversation it may speak in, and nobody should have
    to go and find a chat id to do that. So each entry carries the id together
    with who has spoken in it, which is what makes one Ping recognisable from
    another in a dropdown: "Alex Weber" is your 1:1, "Alex Weber, Bo Lindqvist"
    is the room you don't want answered.

    Grouped in Python rather than with a `payload ->> '_chat_id'` GROUP BY, so it
    works the same on the SQLite the tests run against as on Postgres.
    """
    my_id = _my_user_id(db)
    rows = (
        db.execute(
            select(RawEvent)
            .where(
                RawEvent.type == "ping",
                RawEvent.updated_at
                >= utcnow() - timedelta(days=CONVERSATION_SCAN_DAYS),
            )
            .order_by(RawEvent.id.desc())
            .limit(CONVERSATION_SCAN_LINES)
        )
        .scalars()
        .all()
    )
    out: list[dict] = []
    for chat_id, group in conversation.group_by_thread(rows):
        if chat_id is None:
            continue
        who = conversation.speakers(group, my_id)
        newest = max(group, key=lambda e: as_aware(e.updated_at) or utcnow())
        out.append({
            "chat_id": int(chat_id),
            # Everyone but you who has spoken. Empty means the only lines we hold
            # are your own — say so rather than offer a blank row.
            "who": who,
            "label": ", ".join(who) or "nobody but you has spoken here",
            "last_at": as_aware(newest.updated_at),
            "url": safe_url((newest.payload or {}).get("app_url")),
        })
    out.sort(key=lambda c: c["last_at"] or utcnow(), reverse=True)
    return out


def self_check(db: Session) -> list[dict]:
    """Walk the chain a Ping travels and report what would stop it.

    Ordered the way the message travels — fetched, on the list, allowed to speak
    right now — so the first "problem" line is the one to deal with.
    """
    cfg = runtime.load(db)
    found: list[dict] = []

    def add(level: str, text: str) -> None:
        found.append({"level": level, "text": text})

    if not cfg.autoreply_enabled:
        add("problem", "Auto-reply is switched off in Settings, so nothing is "
                       "read and nothing is drafted.")
    if not settings.poll_pings:
        add("problem", "POLL_PINGS is off in the environment, so Ping messages "
                       "are never fetched in the first place.")
    if _my_user_id(db) is None:
        add("problem", "Basecamp hasn't told us who you are yet, so the butler "
                       "can't tell your own messages from theirs. Reconnect on "
                       "the Settings page.")
    if _state(db, "llm_status") == "unreachable":
        add("problem", f"The LLM at {settings.ollama_url} isn't answering. "
                       "Nothing can be drafted until it is.")

    everyone = db.execute(select(AutoReplyRule)).scalars().all()
    live = enabled_rules(db)
    off = [r.name for r in everyone if not r.enabled]
    if not everyone:
        add("problem", "Nobody is on the auto-reply list, so no Ping can ever "
                       "be answered.")
    elif not live:
        add("problem", "Every name on the list is disabled: " + ", ".join(off))
    elif off:
        add("warn", "On the list but disabled, so never answered: " + ", ".join(off))

    # An enabled rule with no conversation on it looks configured from the list
    # and answers nothing anywhere — exactly the silence this page exists to
    # explain, so it is named before anything about individual messages.
    unaimed = [r.name for r in live.values() if r.chat_id is None]
    if unaimed:
        add("problem", "On the list but not pointed at a conversation, so "
                       "nothing is ever said to them: " + ", ".join(unaimed)
                       + ". Pick the Ping each one may answer in, under "
                         "“Answer in” on their rule.")

    seen = recent_senders(db)
    if not seen:
        add("problem", f"No Ping messages from anybody in the last "
                       f"{LOOKBACK_HOURS}h. If people have been messaging you, "
                       "they aren't reaching the app — that's a polling problem, "
                       "not a reply one.")
    else:
        unknown = [n for n in seen if n.strip().lower() not in live]
        if unknown:
            add("warn", "Pinged you recently but isn't on the list — copy the "
                        "spelling exactly: " + ", ".join(f"“{n}”" for n in unknown))
        for name in seen:
            rule = live.get(name.strip().lower())
            if rule is None:
                continue
            if rule.chat_id is None:
                continue  # already named above, and the mode is moot until it's set
            if rule.mode == "auto":
                add("ok", f"“{name}” is on the list and set to answer "
                          "automatically.")
            else:
                add("warn", f"“{name}” is on the list in *draft* mode, so "
                            "replies wait on this page for you to send rather "
                            "than going to Basecamp.")

        idle = [
            r.name for r in everyone
            if r.enabled and r.name.strip().lower()
            not in {n.strip().lower() for n in seen}
        ]
        if idle:
            add("warn", f"On the list, but nobody spelled that way has pinged "
                        f"you in {LOOKBACK_HOURS}h: " + ", ".join(idle)
                        + ". If they have, the name doesn't match Basecamp's.")

    if cfg.is_quiet_now():
        add("warn", f"It's quiet hours ({cfg.quiet_hours_start:02d}:00–"
                    f"{cfg.quiet_hours_end:02d}:00 {cfg.timezone}). Replies are "
                    "drafted now and sent when the window ends.")
    if cfg.autoreply_daily_limit and sent_today(db) >= cfg.autoreply_daily_limit:
        add("warn", f"The daily ceiling of {cfg.autoreply_daily_limit} is used "
                    "up, so anything new is drafted rather than sent.")

    if not any(f["level"] == "problem" for f in found):
        add("ok", "Nothing is standing in the way — the next message from "
                  "somebody on the list should get an answer.")
    return found


def reconsider(chat_id) -> tuple[bool, str]:
    """Put a conversation's recent messages back in front of the butler.

    The bug this app shipped with moved the watermark past messages it had
    decided not to answer *yet*, and a watermark is one-way — so conversations
    carry lines that were silently marked handled and will never be looked at
    again. This rewinds one conversation to the start of the answerable window,
    which is the only way back for those.

    Bounded by `STALE_AFTER_HOURS` on purpose: rewinding further would only
    surface lines the pass is going to reject as too old anyway.
    """
    with session_scope() as db:
        rows = (
            db.execute(
                select(RawEvent)
                .where(
                    RawEvent.type == "ping",
                    RawEvent.updated_at
                    >= utcnow() - timedelta(hours=STALE_AFTER_HOURS),
                )
                .order_by(RawEvent.id.desc())
                .limit(MAX_EVENTS_PER_PASS)
            )
            .scalars()
            .all()
        )
        # Filtered here rather than in SQL: the JSONB path operator that would
        # do it is Postgres-only, and this list is already capped.
        mine = [e for e in rows if str(conversation.chat_id_of(e)) == str(chat_id)]
        if not mine:
            return False, (
                f"Nothing has been said in that conversation in the last "
                f"{STALE_AFTER_HOURS}h, so there is nothing left to look at."
            )
        newest = max(mine, key=conversation._said_at)
        _set_watermark(db, chat_id, min(e.id for e in mine) - 1)
        _note(db, chat_id, _sender(newest),
              "Put back in front of the butler by hand — it will read this "
              "conversation again on the next pass.")
        activity.record(
            db,
            "reply",
            f"Asked the butler to read the conversation with "
            f"{_sender(newest) or 'someone'} again.",
        )
        return True, "It will read that conversation again within a minute."


def _state(db: Session, key: str) -> str | None:
    row = db.get(AppState, key)
    return (row.value or None) if row else None


def last_pass(db: Session) -> dict | None:
    """How the most recent pass went, or None if one has never run."""
    row = db.get(AppState, LAST_PASS_KEY)
    try:
        rec = json.loads(row.value) if row and row.value else None
    except ValueError:
        rec = None
    if not isinstance(rec, dict):
        return None
    rec["at"] = _parse_stamp(rec.get("at"))
    return rec


def _parse_stamp(value: str | None):
    """Best-effort ISO parse — a hand-edited app_state row must not break a pass."""
    try:
        return as_aware(parse_bc_datetime(value))
    except (TypeError, ValueError):
        return None


# ── the pass ────────────────────────────────────────────────────────────────
def run_pass() -> int:
    """Consider every Ping thread with new lines. Returns replies composed.

    Non-blocking: if a pass is already running this returns 0 rather than
    queueing behind it.
    """
    if not _lock.acquire(blocking=False):
        log.debug("Auto-reply pass already running; skipping this trigger.")
        return 0
    try:
        with session_scope() as db:
            return _run(db)
    except Exception:
        log.exception("Auto-reply pass failed")
        return 0
    finally:
        _lock.release()


def _run(db: Session) -> int:
    cfg = runtime.load(db)
    if not cfg.autoreply_enabled:
        _note_pass(db, "Auto-reply is switched off in Settings.")
        return 0

    # Anything quiet hours held back goes out first, before new work is drafted:
    # the oldest promise gets kept first.
    _release_held(db, cfg)

    rules = enabled_rules(db)
    if not rules:
        _note_pass(
            db,
            "Nobody is on the auto-reply list, so no Ping can be answered.",
        )
        return 0

    my_id = _my_user_id(db)
    owner = owner_name(db)

    # Newest first, then flipped back into chronological order. Ordering ascending
    # and slicing would hand back the *oldest* rows in the window — i.e. drop
    # exactly the lines that might still need an answer.
    rows = (
        db.execute(
            select(RawEvent)
            .where(
                RawEvent.type == "ping",
                RawEvent.updated_at >= utcnow() - timedelta(hours=LOOKBACK_HOURS),
            )
            .order_by(RawEvent.id.desc())
            .limit(MAX_EVENTS_PER_PASS)
        )
        .scalars()
        .all()
    )
    if not rows:
        _note_pass(
            db,
            f"No Ping messages arrived in the last {LOOKBACK_HOURS}h — nothing "
            "to answer. If people have been pinging you, the problem is further "
            "up: check the Pings heartbeat below.",
        )
        return 0
    events = list(reversed(rows))

    # A line with no chat id can't be replied to and can't be watermarked
    # meaningfully — there is one conversation called None and it isn't one.
    threads = [
        (cid, group)
        for cid, group in conversation.group_by_thread(events)
        if cid is not None
    ]
    # Most recently active first: if anything does get left behind by the caps
    # below, it should be the conversation nobody has touched in hours.
    threads.sort(key=lambda t: max(e.id for e in t[1]), reverse=True)

    composed = 0
    drafts_left = MAX_DRAFTS_PER_PASS
    unlisted: list[str] = []
    for chat_id, group in threads[:MAX_THREADS_PER_PASS]:
        try:
            made, spent = _consider(
                db, chat_id, group, rules, my_id, owner, cfg,
                can_draft=drafts_left > 0, unlisted=unlisted,
            )
        except Exception:
            log.exception("Auto-reply: thread %s failed", chat_id)
            continue
        composed += made
        if spent:
            drafts_left -= 1

    if len(threads) > MAX_THREADS_PER_PASS:
        log.info(
            "Auto-reply examined the %d most recently active of %d ping thread(s); "
            "the quieter ones keep their place for a later pass.",
            MAX_THREADS_PER_PASS,
            len(threads),
        )
    _note_unlisted(db, unlisted)
    _note_pass(
        db,
        f"Looked at {min(len(threads), MAX_THREADS_PER_PASS)} Ping "
        f"conversation(s) and composed {composed}. Per-conversation decisions "
        "are below.",
        threads=len(threads),
        composed=composed,
    )
    db.flush()
    return composed


def _consider(
    db: Session,
    chat_id,
    group: list[RawEvent],
    rules: dict[str, AutoReplyRule],
    my_id: int | None,
    owner: str,
    cfg: RuntimeConfig,
    *,
    can_draft: bool = True,
    unlisted: list[str] | None = None,
) -> tuple[int, bool]:
    """Decide about one conversation.

    Returns (replies composed, whether an LLM round trip was spent). Every
    ``return`` below is either final — and moves the watermark — or a deferral
    that deliberately leaves it alone so the next pass sees the same lines.
    """
    newest_id = max(e.id for e in group)
    seen = _watermark(db, chat_id)
    who = _sender(group[-1])

    # First sight of this thread: remember where we are and answer nothing. This
    # is what stops switching the feature on from replying to a week of history.
    if seen is None:
        _set_watermark(db, chat_id, newest_id)
        _note(db, chat_id, who, "First look at this conversation — noted where "
                                "it stands, answered nothing. The next message "
                                "is the first one it can act on.")
        return 0, False

    fresh = [e for e in group if e.id > seen]
    if not fresh:
        # Deliberately leaves the previous decision in place: nothing has
        # happened here, so overwriting it with "nothing happened" would throw
        # away the answer to the question actually being asked.
        return 0, False

    # The newest line being ours means the conversation is already answered.
    if conversation.is_own(fresh[-1], my_id):
        _set_watermark(db, chat_id, newest_id)
        _note(db, chat_id, who, "Your own message is the newest one here — "
                                "nothing left to answer.")
        return 0, False

    inbound = [e for e in fresh if not conversation.is_own(e, my_id)]
    if not inbound:
        _set_watermark(db, chat_id, newest_id)
        _note(db, chat_id, who, "Nothing new from anybody else.")
        return 0, False

    latest = inbound[-1]
    person = _sender(latest)
    rule = rules.get(person.strip().lower())
    if rule is None:
        # Not on the allowlist. Move the watermark on so we don't re-examine
        # these lines forever, and say nothing — this is the common case.
        _set_watermark(db, chat_id, newest_id)
        log.info(
            "Auto-reply: %r isn't on the allow-list — thread %s left alone.",
            person or "an unnamed sender",
            chat_id,
        )
        _note(db, chat_id, person, f"“{person}” isn't on the auto-reply list. "
                                   "The name on a rule has to match Basecamp's "
                                   "spelling exactly.")
        if unlisted is not None:
            unlisted.append(person)
        return 0, False

    # On the list — but a rule speaks in one named conversation and nowhere else.
    # A Ping can have several people in it, and answering the right person in the
    # wrong room is the same mistake as answering the wrong person.
    if rule.chat_id is None:
        _set_watermark(db, chat_id, newest_id)
        log.info(
            "Auto-reply: %r has no conversation set on their rule — thread %s "
            "left alone.",
            person,
            chat_id,
        )
        _note(db, chat_id, person,
              f"“{person}” is on the list, but their rule hasn't been pointed "
              "at a conversation yet, so it can't speak anywhere. Pick this one "
              "on their rule in Settings.")
        return 0, False

    if int(rule.chat_id) != int(chat_id):
        _set_watermark(db, chat_id, newest_id)
        log.info(
            "Auto-reply: %r's rule is set to conversation %s, not %s — left alone.",
            person,
            rule.chat_id,
            chat_id,
        )
        _note(db, chat_id, person,
              f"“{person}” is answered in a different conversation "
              f"(the one with id {rule.chat_id}), not this one, so nothing was "
              "said here.")
        return 0, False

    # ── deferrals ───────────────────────────────────────────────────────────
    # None of these move the watermark: the reason they apply now will stop
    # applying, and the lines have to still be here when it does.
    if pending_draft(db, chat_id):
        log.debug(
            "Auto-reply: thread %s already has a draft waiting; holding the "
            "newer lines until it's sent or discarded.",
            chat_id,
        )
        _note(db, chat_id, person, "A draft for this conversation is already "
                                   "waiting above — the newer messages are held "
                                   "until you send or discard it.")
        return 0, False

    if replied_recently(db, chat_id, cfg.autoreply_cooldown_minutes):
        log.debug(
            "Auto-reply: thread %s answered inside the last %d min; the newer "
            "lines wait for the window to pass.",
            chat_id,
            cfg.autoreply_cooldown_minutes,
        )
        _note(db, chat_id, person, f"Already answered inside the "
                                   f"{cfg.autoreply_cooldown_minutes}-minute "
                                   "window — the newer messages are held until "
                                   "it passes.")
        return 0, False

    # ...but not forever.
    if _too_old(latest):
        _set_watermark(db, chat_id, newest_id)
        activity.record(
            db,
            "reply",
            f"Left the Ping from {person} alone — it went unanswered for over "
            f"{STALE_AFTER_HOURS}h, which is too late to reply as you.",
        )
        _note(db, chat_id, person, f"Went unanswered for over "
                                   f"{STALE_AFTER_HOURS}h — too late to reply "
                                   "as you, so it was let go.")
        return 0, False

    if not can_draft:
        # This pass has spent its LLM budget. Leave the watermark where it is;
        # the thread is genuinely next in line rather than skipped.
        log.debug("Auto-reply: draft budget spent, thread %s waits.", chat_id)
        _note(db, chat_id, person, "This pass ran out of drafting budget — this "
                                   "conversation is next in line.")
        return 0, False

    circle_id = (latest.payload or {}).get("_circle_id")
    context = conversation.prior_context(
        db, chat_id, fresh[0].id, event_type="ping", limit=CONTEXT_LINES
    )
    transcript = conversation.render_transcript(fresh, my_id, context)
    if not transcript.strip():
        _set_watermark(db, chat_id, newest_id)
        _note(db, chat_id, person, "The new messages carry no readable text "
                                   "(an image or a file on its own, perhaps).")
        return 0, False

    from .classifier import ollama

    verdict = ollama.draft_reply(
        transcript,
        owner=owner,
        person=person,
        tone=rule.tone,
        instructions=rule.instructions,
    )
    if verdict is ollama.UNREACHABLE:
        # Leave the watermark where it is so this thread is reconsidered once
        # the LLM host is back. Saying nothing is always the safe outcome here.
        # Counted as spent so one pass doesn't hammer a dead host five times.
        log.info("Auto-reply: LLM unreachable, leaving thread %s for later.", chat_id)
        _note(db, chat_id, person, "The LLM host didn't answer, so nothing was "
                                   "written. The message is still on the books "
                                   "and will be tried again.")
        return 0, True

    _set_watermark(db, chat_id, newest_id)

    if not verdict or not verdict.get("reply"):
        why = (verdict or {}).get("why") or "nothing to answer"
        activity.record(
            db,
            "reply",
            f"Read the Ping from {person} → no reply drafted ({why}).",
            detail=transcript[:4000],
        )
        _note(db, chat_id, person,
              f"The model read it and judged no reply was needed: {why}.")
        return 0, True

    # The model can only see the conversation, and a conversation that has moved
    # on to "haha" gives it nothing new to say — so it reaches for the line it
    # already wrote, which is right there in the transcript. Sending that would
    # put the same message in front of the other person twice.
    if already_said(db, chat_id, verdict["text"]):
        activity.record(
            db,
            "reply",
            f"Read the Ping from {person} → said nothing: the reply it came "
            "up with is word for word one already in that conversation.",
            detail=transcript[:4000],
        )
        _note(db, chat_id, person, "The reply it came up with is word for word "
                                   "one already in this conversation, so it "
                                   "said nothing.")
        return 0, True

    # Decide send-now vs hold, and record *why* it was held so the review page
    # can say so rather than looking like the rule was ignored.
    mode = rule.mode if rule.mode in MODES else "draft"
    held_reason = None
    if mode == "auto":
        if cfg.is_quiet_now():
            held_reason = HELD_QUIET
        elif cfg.autoreply_daily_limit and sent_today(db) >= cfg.autoreply_daily_limit:
            held_reason = (
                f"daily limit of {cfg.autoreply_daily_limit} auto-replies reached"
            )

    reply = AutoReply(
        rule_id=rule.id,
        source_event_id=latest.id,
        circle_id=circle_id,
        chat_id=chat_id,
        person=person,
        incoming=transcript[:8000],
        draft=verdict["text"],
        status="draft",
        mode=mode,
        held_reason=held_reason,
    )
    db.add(reply)
    db.flush()

    url = safe_url((latest.payload or {}).get("app_url"))
    if mode == "auto" and held_reason is None:
        # Record the send *after* it lands. `_deliver` can refuse (a duplicate,
        # a dead connection) and writes its own row when it does — announcing
        # the reply first made the feed claim things that never left the house.
        if _deliver(db, reply):
            activity.record(
                db,
                "reply",
                f"Replied to {person}: “{reply.draft[:120]}”",
                detail=transcript[:4000],
                url=url,
            )
            _note(db, chat_id, person, "Replied.")
        else:
            _note(db, chat_id, person,
                  f"Wrote a reply but couldn't send it: {reply.error}")
    else:
        activity.record(
            db,
            "reply",
            f"Drafted a reply to {person} for you to review"
            + (f" ({held_reason})" if held_reason else "")
            + f": “{reply.draft[:120]}”",
            detail=transcript[:4000],
            url=url,
        )
        _note(db, chat_id, person, "Drafted a reply for you to review"
              + (f" ({held_reason})" if held_reason else "") + ".")
    return 1, True


def _release_held(db: Session, cfg: RuntimeConfig) -> int:
    """Send the `auto` replies quiet hours held back, once the window ends.

    An `auto` rule means "send this without showing me first"; quiet hours are
    about *when*, not *whether*. Held drafts used to sit on /replies until
    somebody noticed them, which for an auto rule is exactly the outcome it was
    set up to avoid. The daily ceiling is deliberately not released this way — a
    ceiling that empties itself an hour later isn't one.

    Anything that fails to send loses its held flag and stays on /replies as an
    ordinary draft, so a permanent failure isn't retried every minute forever.
    """
    if cfg.is_quiet_now():
        return 0
    rows = (
        db.execute(
            select(AutoReply)
            .where(
                AutoReply.status == "draft",
                AutoReply.mode == "auto",
                AutoReply.held_reason.isnot(None),
                AutoReply.created_at >= utcnow() - timedelta(hours=LOOKBACK_HOURS),
            )
            .order_by(AutoReply.created_at.asc())
            .limit(MAX_DRAFTS_PER_PASS)
        )
        .scalars()
        .all()
    )
    released = 0
    for reply in rows:
        # Prefix match, not equality: rows written by earlier versions carry a
        # different wording for the same hold.
        if not (reply.held_reason or "").startswith("quiet hours"):
            continue
        # Turning the rule off during the night has to mean what it says. The
        # draft stays on /replies either way; it just doesn't go out by itself.
        rule = db.get(AutoReplyRule, reply.rule_id) if reply.rule_id else None
        if rule is None or not rule.enabled:
            log.info(
                "Auto-reply: held reply to %s stays a draft — its rule is no "
                "longer enabled.",
                reply.person,
            )
            continue
        if cfg.autoreply_daily_limit and sent_today(db) >= cfg.autoreply_daily_limit:
            break
        ok = _deliver(db, reply)
        reply.held_reason = None
        if ok:
            released += 1
            activity.record(
                db,
                "reply",
                f"Quiet hours are over — sent the held reply to {reply.person}: "
                f"“{reply.draft[:120]}”",
                url=reply.url,
            )
    if released:
        log.info("Auto-reply: released %d held reply/replies after quiet hours.", released)
    return released


# ── sending ─────────────────────────────────────────────────────────────────
def _deliver(db: Session, reply: AutoReply) -> bool:
    """Post `reply` to Basecamp inside the caller's transaction.

    A transport failure is recorded on the row (status ``failed``) rather than
    raised: the draft survives, so you can look at what happened and press Send
    yourself. Every refusal also writes an activity row, because a reply that
    quietly didn't go out is the one you need told about. Returns whether it
    landed.
    """
    if reply.status == "sent":
        return True
    if not reply.circle_id or not reply.chat_id:
        reply.status = "failed"
        reply.error = "No conversation id stored for this reply."
        activity.record(
            db,
            "error",
            f"Couldn't send the reply to {reply.person} — the conversation it "
            "belongs to wasn't recorded. It's still on the Replies page.",
        )
        return False

    # Last line of defence, and the one that covers the paths the compose-time
    # check can't see: a retried pass, a second click on Send, a transaction that
    # rolled back after the message had already left. Basecamp has no notion of
    # an idempotency key, so the check has to live here.
    if already_said(
        db, reply.chat_id, reply.draft, statuses=("sent",), exclude_id=reply.id
    ):
        reply.error = (
            "That exact message is already in this conversation — not sending "
            "it a second time."
        )
        log.info("Auto-reply: refused a duplicate send to chat %s", reply.chat_id)
        activity.record(
            db,
            "reply",
            f"Held back a reply to {reply.person} — that exact message is "
            "already in the conversation.",
        )
        return False

    client = client_for(db)
    if client is None:
        reply.status = "failed"
        reply.error = "Basecamp isn't connected."
        activity.record(
            db,
            "error",
            f"Couldn't send the reply to {reply.person} — Basecamp isn't "
            "connected. It's still on the Replies page.",
        )
        return False
    try:
        created = client.create_chat_line(
            reply.circle_id, reply.chat_id, as_html(reply.draft)
        )
    except Exception as exc:
        log.exception("Auto-reply: could not post to chat %s", reply.chat_id)
        reply.status = "failed"
        reply.error = f"{type(exc).__name__}: {exc}"[:500]
        activity.record(
            db,
            "error",
            f"Couldn't send the reply to {reply.person} — {type(exc).__name__}. "
            "It's still on the Replies page.",
        )
        return False
    finally:
        client.close()

    reply.status = "sent"
    reply.sent_at = utcnow()
    reply.error = None
    reply.basecamp_line_id = created.get("id")
    reply.url = safe_url(created.get("app_url") or created.get("url"))
    # Commit the send on its own, immediately. A posted message can't be
    # recalled, so the record of it must not be able to disappear: if the rest
    # of this pass failed and took the row and the watermark down with it, the
    # next cycle would read the same unanswered lines and post the same message
    # again — the failure the person on the other end actually notices.
    db.commit()
    return True


def send(reply_id: int, text: str | None = None) -> tuple[bool, str]:
    """Send one stored reply, optionally replacing its text first.

    This is what the Send button on /replies calls, and also how an ``auto``
    rule's held draft goes out. Returns (ok, message-for-the-user).
    """
    with session_scope() as db:
        reply = db.get(AutoReply, reply_id)
        if reply is None:
            return False, "That reply is gone."
        if reply.status == "sent":
            return False, "That reply was already sent."
        if text is not None:
            cleaned = text.strip()
            if not cleaned:
                return False, "An empty message can't be sent."
            reply.draft = cleaned[:2000]
        ok = _deliver(db, reply)
        if ok:
            # Only now: a failed send that had already forgotten *why* it was
            # held would come back looking like an ordinary draft.
            reply.held_reason = None
            activity.record(
                db,
                "reply",
                f"Sent your reply to {reply.person}: “{reply.draft[:120]}”",
                url=reply.url,
            )
            return True, "Sent."
        return False, reply.error or "Basecamp refused the message."


def discard(reply_id: int) -> bool:
    """Drop a draft without sending it."""
    with session_scope() as db:
        reply = db.get(AutoReply, reply_id)
        if reply is None or reply.status == "sent":
            return False
        reply.status = "discarded"
        activity.record(db, "reply", f"Discarded the drafted reply to {reply.person}.")
        return True


def regenerate(reply_id: int) -> tuple[bool, str]:
    """Ask the model for a different draft against the same transcript.

    You asked for this one by pressing a button, so a ``reply=false`` verdict
    isn't taken as "say nothing" the way it is on the automatic path — if there
    is text, you get it. What still applies is the rule that outranks everything
    here: it may not hand you something the thread has already heard.
    """
    with session_scope() as db:
        reply = db.get(AutoReply, reply_id)
        if reply is None or reply.status == "sent":
            return False, "That reply can't be redrafted."
        rule = db.get(AutoReplyRule, reply.rule_id) if reply.rule_id else None
        owner = owner_name(db)

        from .classifier import ollama

        verdict = ollama.draft_reply(
            reply.incoming or "",
            owner=owner,
            person=reply.person or "",
            tone=rule.tone if rule else None,
            instructions=rule.instructions if rule else None,
        )
        if verdict is ollama.UNREACHABLE:
            return False, "The LLM host isn't reachable right now."
        if not verdict or not verdict.get("text"):
            return False, "The model didn't return anything usable."
        if already_said(db, reply.chat_id, verdict["text"], exclude_id=reply.id):
            return False, (
                "The model came back with wording that's already in this "
                "conversation — try again, or edit the draft yourself."
            )
        reply.draft = verdict["text"]
        reply.status = "draft"
        return True, verdict["text"]
