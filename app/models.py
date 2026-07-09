"""Postgres data model."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


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
    payload: Mapped[dict] = mapped_column(JSONB)
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
    # Deep link back into Basecamp, when we have one.
    source_url: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

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
