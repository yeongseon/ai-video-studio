"""Persist optional project settings without changing legacy rows."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("creator_projects", sa.Column(
        "settings", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("creator_projects", "settings")
