"""Answer Basecamp Pings on your behalf — carefully.

Everything else in this app writes into *your* copy of the world: a suggestion
on the dashboard, a to-do in a list you nominated. This module is the first one
that puts words in your mouth in front of somebody else, so it is built to be
hard to fire by accident and easy to audit afterwards.

The constraints, in the order they're enforced:

  * **Off unless asked.** `autoreply_enabled` is off by default.
  * **Allowlist only.** A Ping is only ever answered if an `AutoReplyRule` names
    its sender. No rule, no reply — there is no "reply to everyone" setting.
  * **Direct messages only.** Campfire (group chat) and project messages are
    never answered; a room full of people is not a conversation you can safely
    autopilot.
  * **Draft by default.** A rule's mode decides what happens to the drafted
    text: ``draft`` parks it on /replies for you to read and send, ``auto``
    posts it straight away. New rules are created as drafts.
  * **No backlog.** First sight of a thread only records a watermark, so
    switching the feature on never fires a volley of replies at old messages.
  * **Bounded.** One reply per thread per cooldown window, a hard daily ceiling
    across all threads, and nothing auto-sent during quiet hours — an ``auto``
    rule degrades to a draft rather than pushing past a limit.
  * **Never the last word.** If the newest line in the thread is already ours,
    there's nothing to answer.
  * **Never twice.** Wording that is already in a thread is never posted into
    it again, however the pass arrived at it.

Every composed reply — sent, drafted, discarded or failed — is stored in
`autoreplies`, so "what has it said in my name?" has one answer.
"""
from __future__ import annotations

import html
import logging
import threading
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import activity, runtime
from .basecamp.client import client_for
from .classifier import conversation
from .classifier.rules import _my_user_id
from .db import session_scope
from .models import AppState, AutoReply, AutoReplyRule, RawEvent
from .runtime import RuntimeConfig
from .util import safe_url, utcnow

log = logging.getLogger(__name__)

# Per-thread high-water mark: the highest raw_events.id this module has looked
# at for that conversation. Same scheme the poller uses for ingestion, kept
# separate so the two can't interfere.
CP_PREFIX = "autoreply_cp_"

MODES = ("draft", "auto")

# How many earlier lines of the thread to show the model for context.
CONTEXT_LINES = 8

# A pass can hold the lock for a while (one LLM round trip per thread), so cap
# how many conversations one sweep will consider.
MAX_THREADS_PER_PASS = 10

# How far back a pass looks for ping lines. A reply to something said two days
# ago isn't worth sending, and this keeps the query off the whole (retained)
# history of the fastest-growing table in the schema.
LOOKBACK_HOURS = 24

# How far back the duplicate check looks for wording we have already used in a
# thread. Longer than the cooldown on purpose: the cooldown is about how often
# it is polite to speak, this is about never saying the same thing twice.
DUPLICATE_WINDOW_HOURS = 24

# Only one pass at a time — the poll cycle triggers it, and a slow LLM must not
# let two passes read the same watermark and answer the same thread twice.
_lock = threading.Lock()


# ── helpers ─────────────────────────────────────────────────────────────────
def enabled_rules(db: Session) -> dict[str, AutoReplyRule]:
    """Enabled rules keyed by lower-cased name, as senders are matched."""
    rows = db.execute(
        select(AutoReplyRule).where(AutoReplyRule.enabled.is_(True))
    ).scalars()
    return {(r.name or "").strip().lower(): r for r in rows if (r.name or "").strip()}


def _sender(event: RawEvent) -> str:
    return ((event.payload or {}).get("creator") or {}).get("name", "") or ""


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


def as_html(text: str) -> str:
    """Turn a drafted reply into the rich text Basecamp expects.

    Chat lines are HTML. The body here was written by a language model out of
    someone else's message, so the three characters that carry structure —
    ``&``, ``<`` and ``>`` — are escaped rather than trusted: otherwise a ``<``
    in the reply silently swallows the rest of it, and anything more deliberate
    in the incoming message could be echoed back as live markup.

    Quotes and apostrophes are deliberately left alone. They only need escaping
    inside an attribute value, and we never build one — while Basecamp does not
    decode the entity on the way back out, so escaping them puts the entity
    itself in front of the reader: "it&#x27;s" where you wrote "it's".
    """
    escaped = html.escape(text.strip(), quote=False)
    return "<br>".join(line for line in escaped.split("\n"))


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


def replied_recently(db: Session, chat_id, cooldown_minutes: int) -> bool:
    """True if we already composed a reply for this thread inside the window.

    Deliberately counts drafts as well as sends: an unread draft sitting on the
    /replies page means the conversation is already waiting on you, and stacking
    a second draft on top of it helps nobody.
    """
    if cooldown_minutes <= 0:
        return False
    since = utcnow() - timedelta(minutes=cooldown_minutes)
    row = db.execute(
        select(AutoReply.id)
        .where(
            AutoReply.chat_id == chat_id,
            AutoReply.status.in_(("draft", "sent")),
            AutoReply.created_at >= since,
        )
        .limit(1)
    ).first()
    return row is not None


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
        return 0
    rules = enabled_rules(db)
    if not rules:
        return 0

    my_id = _my_user_id(db)
    owner = owner_name(db)

    events = (
        db.execute(
            select(RawEvent)
            .where(
                RawEvent.type == "ping",
                RawEvent.updated_at >= utcnow() - timedelta(hours=LOOKBACK_HOURS),
            )
            .order_by(RawEvent.id.asc())
            .limit(500)
        )
        .scalars()
        .all()
    )
    if not events:
        return 0

    composed = 0
    threads = conversation.group_by_thread(events)
    for chat_id, group in threads[:MAX_THREADS_PER_PASS]:
        try:
            composed += _consider(db, chat_id, group, rules, my_id, owner, cfg)
        except Exception:
            log.exception("Auto-reply: thread %s failed", chat_id)
    if len(threads) > MAX_THREADS_PER_PASS:
        log.info(
            "Auto-reply looked at %d of %d ping thread(s) this pass; the rest "
            "follow next cycle.",
            MAX_THREADS_PER_PASS,
            len(threads),
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
) -> int:
    """Decide about one conversation. Returns 1 if a reply was composed."""
    newest_id = max(e.id for e in group)
    seen = _watermark(db, chat_id)

    # First sight of this thread: remember where we are and answer nothing. This
    # is what stops switching the feature on from replying to a week of history.
    if seen is None:
        _set_watermark(db, chat_id, newest_id)
        return 0

    fresh = [e for e in group if e.id > seen]
    if not fresh:
        return 0

    # The newest line being ours means the conversation is already answered.
    if conversation.is_own(fresh[-1], my_id):
        _set_watermark(db, chat_id, newest_id)
        return 0

    inbound = [e for e in fresh if not conversation.is_own(e, my_id)]
    if not inbound:
        _set_watermark(db, chat_id, newest_id)
        return 0

    latest = inbound[-1]
    person = _sender(latest)
    rule = rules.get(person.strip().lower())
    if rule is None:
        # Not on the allowlist. Move the watermark on so we don't re-examine
        # these lines forever, and say nothing — this is the common case.
        _set_watermark(db, chat_id, newest_id)
        return 0

    if replied_recently(db, chat_id, cfg.autoreply_cooldown_minutes):
        _set_watermark(db, chat_id, newest_id)
        activity.record(
            db,
            "reply",
            f"Left the Ping from {person} alone — already replied to that "
            f"conversation in the last {cfg.autoreply_cooldown_minutes} min.",
        )
        return 0

    circle_id = (latest.payload or {}).get("_circle_id")
    context = conversation.prior_context(
        db, chat_id, fresh[0].id, event_type="ping", limit=CONTEXT_LINES
    )
    transcript = conversation.render_transcript(fresh, my_id, context)
    if not transcript.strip():
        _set_watermark(db, chat_id, newest_id)
        return 0

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
        log.info("Auto-reply: LLM unreachable, leaving thread %s for later.", chat_id)
        return 0

    _set_watermark(db, chat_id, newest_id)

    if not verdict or not verdict.get("reply"):
        why = (verdict or {}).get("why") or "nothing to answer"
        activity.record(
            db,
            "reply",
            f"Read the Ping from {person} → no reply drafted ({why}).",
            detail=transcript[:4000],
        )
        return 0

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
        return 0

    # Decide send-now vs hold, and record *why* it was held so the review page
    # can say so rather than looking like the rule was ignored.
    mode = rule.mode if rule.mode in MODES else "draft"
    held_reason = None
    if mode == "auto":
        if cfg.is_quiet_now():
            held_reason = "quiet hours — held for you to send"
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
        activity.record(
            db,
            "reply",
            f"Replying to {person}: “{reply.draft[:120]}”",
            detail=transcript[:4000],
            url=url,
        )
        _deliver(db, reply)
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
    return 1


# ── sending ─────────────────────────────────────────────────────────────────
def _deliver(db: Session, reply: AutoReply) -> bool:
    """Post `reply` to Basecamp inside the caller's transaction.

    A transport failure is recorded on the row (status ``failed``) rather than
    raised: the draft survives, so you can look at what happened and press Send
    yourself. Returns whether it landed.
    """
    if reply.status == "sent":
        return True
    if not reply.circle_id or not reply.chat_id:
        reply.status = "failed"
        reply.error = "No conversation id stored for this reply."
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
        return False

    client = client_for(db)
    if client is None:
        reply.status = "failed"
        reply.error = "Basecamp isn't connected."
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
        reply.held_reason = None
        ok = _deliver(db, reply)
        if ok:
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
    """Ask the model for a different draft against the same transcript."""
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
        reply.draft = verdict["text"]
        reply.status = "draft"
        return True, verdict["text"]
