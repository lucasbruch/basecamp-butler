"""todos.due_all_day — tell a calendar day apart from an instant

`due_date` holds two different kinds of value: a Basecamp ``due_on`` (a bare
date, stored as midnight UTC) and a schedule entry's ``starts_at`` (a real
instant). Rendering both through a timezone conversion pushed every bare due
date onto the previous day for anyone west of UTC. This flag says which one a
row is holding so the display layer can convert only the instants.

Existing rows are backfilled as all-day when the stored time is exactly
midnight UTC, which is what the ``due_on`` parse produced. A schedule entry that
genuinely starts at 00:00 UTC gets mislabelled by that heuristic; it costs at
most a date shown in UTC rather than local, and there's no other signal in the
row to go on.

Revision ID: 0003_due_all_day
Revises: 0002_todo_lifecycle
Create Date: 2026-07-31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_due_all_day"
down_revision: Union[str, None] = "0002_todo_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    # Guarded the same way as 0002: the table may already have been built by
    # `create_all` from a newer models.py, and re-adding would abort the run.
    if "due_all_day" in _columns("todos"):
        return
    op.add_column(
        "todos",
        sa.Column(
            "due_all_day",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "UPDATE todos SET due_all_day = true "
            "WHERE due_date IS NOT NULL "
            "AND EXTRACT(HOUR FROM due_date AT TIME ZONE 'UTC') = 0 "
            "AND EXTRACT(MINUTE FROM due_date AT TIME ZONE 'UTC') = 0"
        )


def downgrade() -> None:
    op.drop_column("todos", "due_all_day")
