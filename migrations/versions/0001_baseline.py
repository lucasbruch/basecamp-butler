"""baseline schema

Snapshots the current model definitions as the first revision. It's written to
be idempotent (checkfirst) so it works both ways:

  * a brand-new database → creates every table
  * an existing database whose tables were created by the old
    ``Base.metadata.create_all`` path → creates nothing, just stamps this
    revision so future migrations have a baseline to build on.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-10
"""
from typing import Sequence, Union

from alembic import op

# Import the app models so Base.metadata is fully populated.
from app.db import Base
from app import models  # noqa: F401

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
