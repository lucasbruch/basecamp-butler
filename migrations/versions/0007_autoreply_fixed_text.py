"""autoreply_rules.prefix / .suffix — fixed text around the drafted reply

`tone` and `instructions` are requests: they go into the prompt and the model
obliges or doesn't. That's fine for voice, and useless for the things that have
to be in the message every single time — a standing disclaimer that a reply was
drafted for you, a sign-off, a ticket link. Those are pasted on rather than
asked for, which is what these two columns hold.

Both are per rule, alongside the tone they sit next to, and both default to
null — an upgraded database wraps nothing around anything until you say so.

Revision ID: 0007_autoreply_fixed_text
Revises: 0006_drop_last_polled_at
Create Date: 2026-09-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_autoreply_fixed_text"
down_revision: Union[str, None] = "0006_drop_last_polled_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # Guarded like the migrations before it: a fresh database built by
    # `create_all` from a newer models.py already has both columns, and adding
    # one twice aborts the run. An empty set means there's no table to alter.
    columns = _columns("autoreply_rules")
    if not columns:
        return
    for name in ("prefix", "suffix"):
        if name not in columns:
            op.add_column("autoreply_rules", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    columns = _columns("autoreply_rules")
    for name in ("prefix", "suffix"):
        if name in columns:
            op.drop_column("autoreply_rules", name)
