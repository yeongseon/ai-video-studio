"""Allow the deletion barrier; schema downgrade preserves in-flight rows as archived."""

from collections.abc import Sequence

from alembic import op

revision: str = "051"
down_revision: str | None = "050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("creator_projects") as batch:
        batch.drop_constraint("ck_creator_projects_status", type_="check")
        batch.create_check_constraint(
            "ck_creator_projects_status",
            "status IN ('draft', 'active', 'completed', 'archived', 'deleting')",
        )


def downgrade() -> None:
    # Preserve interrupted deletion rows; archived does not imply successful cleanup.
    op.execute("UPDATE creator_projects SET status = 'archived' WHERE status = 'deleting'")
    with op.batch_alter_table("creator_projects") as batch:
        batch.drop_constraint("ck_creator_projects_status", type_="check")
        batch.create_check_constraint(
            "ck_creator_projects_status", "status IN ('draft', 'active', 'completed', 'archived')",
        )
