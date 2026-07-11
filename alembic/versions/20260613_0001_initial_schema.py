from __future__ import annotations

from alembic import op

from app import models  # noqa: F401
from app.db import Base, SCHEMA_COMPAT_SQL

revision = "20260613_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    for statement in SCHEMA_COMPAT_SQL:
        op.execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
