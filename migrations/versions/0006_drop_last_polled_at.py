"""projects.last_polled_at — drop a column that never held per-project news

Every poll cycle stamped this onto *every* enabled project, unconditionally,
whether that project had produced anything or not. All the rows were written in
one loop from one pass, so they only ever held the same instant: it was the
global poll heartbeat wearing a per-project costume, and the one thing it did
encode was misleading, since a project switched off kept whatever stamp it had
when it was last on.

The cost was real. Twenty projects times a five-minute poll is ~5,760 UPDATEs a
day against a twenty-row table, none of which told anyone anything that
`app_state["last_poll_at"]` did not already say. The Settings page now reads that
single heartbeat instead, and says "not polled" for the projects it is not true
of.

Revision ID: 0006_drop_last_polled_at
Revises: 0005_autoreply_chat_id
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_drop_last_polled_at"
down_revision: Union[str, None] = "0005_autoreply_chat_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set:
    insp = sa.inspect(op.get_bind())
    if table not in set(insp.get_table_names()):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    # Guarded like 0002-0005: a fresh database built by `create_all` from the
    # current models.py never had the column, and dropping it aborts the run.
    if "last_polled_at" in _columns("projects"):
        op.drop_column("projects", "last_polled_at")


def downgrade() -> None:
    # Comes back empty. The values are not worth preserving — see above, they
    # were all the same instant — and the next poll no longer writes them.
    columns = _columns("projects")
    if columns and "last_polled_at" not in columns:
        op.add_column(
            "projects",
            sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        )
