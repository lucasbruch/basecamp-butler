"""to-do lifecycle, snooze, write-back, reports, muted senders

Adds everything the 0001 baseline didn't have:

  * ``todos``: updated_at / completed_at (so "what did I close this week" is
    answerable), snoozed_until, thread_key (burst coalescing), and the
    basecamp_todo_id / basecamp_url pair used by write-back.
  * ``projects``: the write-back target to-do list.
  * new ``reports`` and ``muted_senders`` tables.
  * indexes that matter at size: todos.created_at, todos.thread_key,
    todos.snoozed_until, and an expression index on the chat id buried in
    raw_events.payload (queried on every LLM thread classification).

Every step is guarded so this is safe to run against a database whose tables
were created by ``create_all`` from a newer models.py — the columns may already
exist, and re-adding them would abort the whole migration.

Revision ID: 0002_todo_lifecycle
Revises: 0001_baseline
Create Date: 2026-07-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_todo_lifecycle"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {i["name"] for i in inspector.get_indexes(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _add(table: str, column: sa.Column, existing: set[str]) -> None:
    if column.name not in existing:
        op.add_column(table, column)


def upgrade() -> None:
    todos = _columns("todos")
    _add("todos", sa.Column("updated_at", sa.DateTime(timezone=True)), todos)
    _add("todos", sa.Column("completed_at", sa.DateTime(timezone=True)), todos)
    _add("todos", sa.Column("snoozed_until", sa.DateTime(timezone=True)), todos)
    _add("todos", sa.Column("thread_key", sa.String(100)), todos)
    _add("todos", sa.Column("basecamp_todo_id", sa.BigInteger()), todos)
    _add("todos", sa.Column("basecamp_url", sa.String(1000)), todos)

    # Existing rows get updated_at seeded from created_at so ordering by it is
    # sane immediately rather than after everything has been touched once.
    if "updated_at" not in todos and "todos" in _tables():
        op.execute("UPDATE todos SET updated_at = created_at WHERE updated_at IS NULL")

    projects = _columns("projects")
    _add("projects", sa.Column("todolist_id", sa.BigInteger()), projects)
    _add("projects", sa.Column("todolist_name", sa.String(500)), projects)

    tables = _tables()
    if "reports" not in tables:
        op.create_table(
            "reports",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("hours", sa.Integer(), nullable=False),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("model", sa.String(100)),
            sa.Column("event_count", sa.Integer()),
            sa.Column("todo_count", sa.Integer()),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("scheduled", sa.Boolean()),
        )
        op.create_index("ix_reports_created_at", "reports", ["created_at"])

    if "muted_senders" not in tables:
        op.create_table(
            "muted_senders",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("name", sa.String(200), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    todo_indexes = _indexes("todos")
    for name, column in (
        ("ix_todos_created_at", "created_at"),
        ("ix_todos_thread_key", "thread_key"),
        ("ix_todos_snoozed_until", "snoozed_until"),
    ):
        if name not in todo_indexes:
            op.create_index(name, "todos", [column])

    # Expression index — Postgres only, and only worth it there (the app runs on
    # Postgres; the test suite never reaches this migration).
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_raw_events_chat_id "
            "ON raw_events ((payload ->> '_chat_id'))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_raw_events_chat_id")
    for name in (
        "ix_todos_snoozed_until",
        "ix_todos_thread_key",
        "ix_todos_created_at",
    ):
        op.drop_index(name, table_name="todos")
    op.drop_table("muted_senders")
    op.drop_index("ix_reports_created_at", table_name="reports")
    op.drop_table("reports")
    op.drop_column("projects", "todolist_name")
    op.drop_column("projects", "todolist_id")
    for name in (
        "basecamp_url",
        "basecamp_todo_id",
        "thread_key",
        "snoozed_until",
        "completed_at",
        "updated_at",
    ):
        op.drop_column("todos", name)
