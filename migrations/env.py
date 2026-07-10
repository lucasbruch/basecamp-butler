"""Alembic environment. Wires migrations to the app's metadata + DATABASE_URL.

The URL comes from app.config.settings (not alembic.ini) so there's exactly one
place that defines where the database lives.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the app so every model registers on Base.metadata before autogenerate.
from app.config import settings
from app.db import Base
from app import models  # noqa: F401  (registers tables)

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence the app's
    # (and uvicorn's) already-configured loggers when init_db() runs this in
    # process — turning the container logs dead after the first migration. Keep
    # them alive; we only want to *add* Alembic's logging config, not replace it.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
