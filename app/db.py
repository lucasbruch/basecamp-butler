"""SQLAlchemy engine + session helpers."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)

# Repo root (parent of the `app` package) — where alembic.ini / migrations live.
_ROOT = Path(__file__).resolve().parent.parent

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Bring the schema up to date by running Alembic migrations to head.

    The baseline revision is idempotent, so this is safe on both fresh databases
    and ones whose tables predate migrations. If Alembic can't be loaded for some
    reason, fall back to a plain create_all so a fresh install still boots.
    """
    from . import models  # noqa: F401  (register tables on Base.metadata)

    try:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(_ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(cfg, "head")
    except Exception:
        log.exception("Alembic upgrade failed — falling back to create_all()")
        Base.metadata.create_all(bind=engine)
