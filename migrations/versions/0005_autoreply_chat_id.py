"""autoreply_rules.chat_id — pin a rule to one Ping conversation

A Ping is not always a direct message: Basecamp lets you start one with several
people in it, and the auto-reply allowlist matched on the *sender* alone. So a
colleague on the list saying something in a five-person Ping got answered, in
the owner's voice, in front of all five.

Naming the conversation is the fix. `chat_id` is Basecamp's chat id — the same
one `ping_cp_<id>`, `autoreply_cp_<id>` and `autoreplies.chat_id` are already
keyed by — and a rule only ever speaks in the conversation it names. Null means
the rule has never been pointed at one, which answers nothing: existing rules
land switched off in effect, and have to be aimed before they speak again. That
is the right way round for anyone upgrading, since being answered in a room is
what brings you here.

Revision ID: 0005_autoreply_chat_id
Revises: 0004_autoreply
Create Date: 2026-08-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_autoreply_chat_id"
down_revision: Union[str, None] = "0004_autoreply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # Guarded like 0002/0003/0004: a fresh database built by `create_all` from a
    # newer models.py already has the column, and adding it again aborts the run.
    # An empty set also means "no such table", which 0004 would have made.
    columns = _columns("autoreply_rules")
    if not columns:
        return
    if "chat_id" not in columns:
        op.add_column(
            "autoreply_rules", sa.Column("chat_id", sa.BigInteger(), nullable=True)
        )
        op.create_index(
            "ix_autoreply_rules_chat_id", "autoreply_rules", ["chat_id"]
        )
    # A short-lived version of this change tried to tell a group Ping apart by
    # who had spoken in it. That can't be made safe — a group where only one
    # person has talked is indistinguishable from a direct message — so it was
    # replaced by the pin above. Dropped here for anyone who ran that build.
    if "direct_only" in columns:
        op.drop_column("autoreply_rules", "direct_only")


def downgrade() -> None:
    if "chat_id" in _columns("autoreply_rules"):
        op.drop_index("ix_autoreply_rules_chat_id", table_name="autoreply_rules")
        op.drop_column("autoreply_rules", "chat_id")
