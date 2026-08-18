"""Postgres data model."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSONB in production; plain JSON everywhere else so the schema can be built on
# SQLite for tests. Same Python-side behaviour, and Postgres still gets the
# indexable binary representation the queries rely on.
PayloadJSON = JSONB().with_variant(JSON(), "sqlite")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Basecamp id
    name: Mapped[str] = mapped_column(String(500))
    # Per-project "auto-add": suggestions land as confirmed instead of suggested.
    auto_add: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether the poller should look at this project at all.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Write-back target: the Basecamp to-do list that confirmed suggestions get
    # pushed into for this project. Null = don't write back for this project.
    todolist_id: Mapped[int | None] = mapped_column(BigInteger)
    todolist_name: Mapped[str | None] = mapped_column(String(500))


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        UniqueConstraint("type", "basecamp_id", "updated_at", name="uq_raw_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # todo | message | comment | chat | todolist ...
    type: Mapped[str] = mapped_column(String(50), index=True)
    basecamp_id: Mapped[int] = mapped_column(BigInteger, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(PayloadJSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class Todo(Base):
    __tablename__ = "todos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # null source => manually added
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_events.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    title: Mapped[str] = mapped_column(String(1000))
    notes: Mapped[str | None] = mapped_column(Text)
    # suggested | confirmed | dismissed | done
    status: Mapped[str] = mapped_column(String(20), default="suggested", index=True)
    # Why the classifier raised this (rule name / LLM), for transparency in the UI.
    reason: Mapped[str | None] = mapped_column(String(500))
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # True when `due_date` names a calendar day rather than an instant — a
    # Basecamp `due_on`, stored as midnight UTC. Such a value must NOT be
    # converted into the display zone; see `util.due_on`.
    due_all_day: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    # Deep link back into Basecamp, when we have one.
    source_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    # Bumped on every status change, so "what did I close this week" is answerable.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Snooze: hidden from the dashboard until this moment, then it resurfaces
    # (and a reminder fires). Null = not snoozed.
    snoozed_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    # The chat thread this came from (Campfire room / Ping conversation id), used
    # to coalesce a burst of messages in one thread into a single suggestion
    # instead of one per poll cycle. Null for non-chat sources.
    thread_key: Mapped[str | None] = mapped_column(String(100), index=True)

    # Write-back: set once the suggestion has been pushed into Basecamp as a real
    # to-do, so we never create it twice.
    basecamp_todo_id: Mapped[int | None] = mapped_column(BigInteger)
    basecamp_url: Mapped[str | None] = mapped_column(String(1000))

    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="todo", cascade="all, delete-orphan"
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    todo_id: Mapped[int] = mapped_column(
        ForeignKey("todos.id", ondelete="CASCADE"), index=True
    )
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    channel: Mapped[str] = mapped_column(String(30), default="telegram")

    todo: Mapped["Todo"] = relationship(back_populates="reminders")


class OAuthToken(Base):
    """Single-row table holding the current Basecamp tokens + account info."""

    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    api_href: Mapped[str | None] = mapped_column(String(500))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Checkpoint(Base):
    """High-water mark per Basecamp recording type (updated_at based change detection)."""

    __tablename__ = "checkpoints"

    resource_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    last_seen_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )


class AppState(Base):
    """Generic key/value store for small bits of runtime state (e.g. my user id)."""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


# `conversation.prior_context` filters on the chat id inside the payload for
# every LLM thread classification — without an index that's a sequential scan of
# the fastest-growing table in the schema. It's an expression index, so it only
# exists on Postgres; `execute_if` keeps create_all working on SQLite.
event.listen(
    RawEvent.__table__,
    "after_create",
    DDL(
        "CREATE INDEX IF NOT EXISTS ix_raw_events_chat_id "
        "ON raw_events ((payload ->> '_chat_id'))"
    ).execute_if(dialect="postgresql"),
)


class Report(Base):
    """A generated activity briefing, kept so you can look back at what you
    missed rather than only ever seeing the one you just generated."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    hours: Mapped[int] = mapped_column(Integer)
    # llm | summary | empty — how the body was produced.
    source: Mapped[str] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(100))
    event_count: Mapped[int] = mapped_column(Integer, default=0)
    todo_count: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text)
    # True when produced by the daily schedule rather than the Generate button.
    scheduled: Mapped[bool] = mapped_column(Boolean, default=False)


class MutedSender(Base):
    """People whose messages never raise a suggestion.

    Matched case-insensitively against the Basecamp `creator.name` on an event —
    the automation account that posts every deploy, the colleague whose Campfire
    stream is pure chatter.
    """

    __tablename__ = "muted_senders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class ActivityLog(Base):
    """Human-readable trace of what the butler is doing — powers the /activity page.

    Deliberately plain-English (`summary`) with an optional expandable `detail`
    (e.g. the exact text sent to the LLM and its raw verdict), so a non-developer
    can see it read a Ping and what it decided, without grepping container logs.
    """

    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    # poll | ping | campfire | llm | rule | notify | error
    kind: Mapped[str] = mapped_column(String(30), index=True)
    summary: Mapped[str] = mapped_column(String(1000))
    detail: Mapped[str | None] = mapped_column(Text)
    # Optional deep link back into Basecamp (e.g. the Ping / recording).
    url: Mapped[str | None] = mapped_column(String(1000))


class AutoReplyRule(Base):
    """Who the butler may answer on your behalf in a Ping, and in what voice.

    This is an allowlist and nothing else: a message only ever gets a reply if
    a rule names its sender. `mode` decides what happens with the drafted text —
    ``draft`` holds it on the /replies page for you to read and send, ``auto``
    posts it to Basecamp immediately. Draft is the default because the failure
    mode of the other one is a colleague receiving something you never wrote.
    """

    __tablename__ = "autoreply_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Basecamp display name, matched case-insensitively (as with MutedSender).
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # Free text handed to the LLM: "warm but brief, first names, no emoji".
    tone: Mapped[str | None] = mapped_column(String(500))
    # Standing instructions for this person ("never commit to a date").
    instructions: Mapped[str | None] = mapped_column(Text)
    # draft | auto
    mode: Mapped[str] = mapped_column(String(10), default="draft")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )


class AutoReply(Base):
    """One drafted (and possibly sent) reply to a Ping conversation.

    Every reply the butler composes lands here first, whatever the rule's mode —
    so "what has it said in my name?" is answerable from one table, and an
    auto-sent message is as reviewable after the fact as a draft is before.
    """

    __tablename__ = "autoreplies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("autoreply_rules.id", ondelete="SET NULL"), nullable=True
    )
    source_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("raw_events.id", ondelete="SET NULL"), nullable=True
    )
    # Where to post: Pings live in Circle buckets, addressed like any chat.
    circle_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    person: Mapped[str | None] = mapped_column(String(200))
    # The transcript the reply was written against, shown when reviewing it.
    incoming: Mapped[str | None] = mapped_column(Text)
    draft: Mapped[str] = mapped_column(Text)
    # draft | sent | discarded | failed
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    # What the rule asked for at the time — an auto rule that produced a draft
    # (quiet hours, daily cap) records why in `held_reason`.
    mode: Mapped[str | None] = mapped_column(String(10))
    held_reason: Mapped[str | None] = mapped_column(String(200))
    error: Mapped[str | None] = mapped_column(String(500))
    basecamp_line_id: Mapped[int | None] = mapped_column(BigInteger)
    url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
