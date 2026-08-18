"""autoreply_rules + autoreplies — answering Pings on the owner's behalf

Two tables, both new, so this is a plain create with no backfill:

  * ``autoreply_rules`` is the allowlist. No row for a sender means that sender
    is never answered, which is what keeps the feature inert until it's asked
    for.
  * ``autoreplies`` is the record of every reply the butler composed — drafted,
    sent, discarded or failed alike — so what went out in your name is
    auditable after the fact, not just before.

Guarded the same way as 0002/0003: the tables may already exist because a newer
models.py was built by ``create_all`` on a fresh database, and re-creating would
abort the run.

Revision ID: 0004_autoreply
Revises: 0003_due_all_day
Create Date: 2026-08-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_autoreply"
down_revision: Union[str, None] = "0003_due_all_day"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()

    if "autoreply_rules" not in existing:
        op.create_table(
            "autoreply_rules",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("tone", sa.String(length=500), nullable=True),
            sa.Column("instructions", sa.Text(), nullable=True),
            sa.Column("mode", sa.String(length=10), nullable=False,
                      server_default="draft"),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
        )

    if "autoreplies" not in existing:
        op.create_table(
            "autoreplies",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("rule_id", sa.Integer(), nullable=True),
            sa.Column("source_event_id", sa.Integer(), nullable=True),
            sa.Column("circle_id", sa.BigInteger(), nullable=True),
            sa.Column("chat_id", sa.BigInteger(), nullable=True),
            sa.Column("person", sa.String(length=200), nullable=True),
            sa.Column("incoming", sa.Text(), nullable=True),
            sa.Column("draft", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False,
                      server_default="draft"),
            sa.Column("mode", sa.String(length=10), nullable=True),
            sa.Column("held_reason", sa.String(length=200), nullable=True),
            sa.Column("error", sa.String(length=500), nullable=True),
            sa.Column("basecamp_line_id", sa.BigInteger(), nullable=True),
            sa.Column("url", sa.String(length=1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["rule_id"], ["autoreply_rules.id"],
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_event_id"], ["raw_events.id"],
                                    ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_autoreplies_chat_id", "autoreplies", ["chat_id"])
        op.create_index("ix_autoreplies_status", "autoreplies", ["status"])
        op.create_index("ix_autoreplies_created_at", "autoreplies", ["created_at"])


def downgrade() -> None:
    op.drop_table("autoreplies")
    op.drop_table("autoreply_rules")
