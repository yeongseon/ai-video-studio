import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from creator_domain.models.project import Project

from project_deletion_support import ROOT, DeletionDatabase, deletion_database


@pytest.mark.asyncio
async def test_downgrade_maps_deleting_when_restoring_previous_constraint(
    deletion_database: DeletionDatabase,
) -> None:
    # Given a deleting row on the upgraded schema.
    pool = deletion_database.pool
    await pool.execute("UPDATE creator_projects SET status='deleting' WHERE id=1")
    # When reverting only this migration.
    deletion_database.migrate("downgrade", "050")
    # Then the row survives as archived and the old CHECK rejects deleting again.
    assert await pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "archived"
    import asyncpg
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("UPDATE creator_projects SET status='deleting' WHERE id=1")
    assert await pool.fetchval("SELECT status FROM creator_projects WHERE id=2") == "draft"


@pytest.mark.asyncio
async def test_previous_release_reads_work_when_schema_accepts_deleting(
    deletion_database: DeletionDatabase,
) -> None:
    # Given an upgraded schema containing both old and newly permitted statuses.
    pool = deletion_database.pool
    await pool.execute("UPDATE creator_projects SET status='deleting' WHERE id=1")
    # When executing the exact previous-release scoped fetch/list/count queries.
    row = await pool.fetchrow("SELECT * FROM creator_projects WHERE id = $1 AND workspace_id = $2", 1, 1)
    rows = await pool.fetch("""
        SELECT * FROM creator_projects WHERE workspace_id = $1
        ORDER BY created_at DESC, id DESC LIMIT $2 OFFSET $3
    """, 1, 20, 0)
    count = await pool.fetchval("SELECT COUNT(*) AS count FROM creator_projects WHERE workspace_id = $1", 1)
    # Then the existing domain decoder and SQL read contract work unchanged.
    assert row is not None
    assert Project.model_validate(dict(row)).status == "deleting"
    assert len(rows) == count == 1


def test_sqlite_upgrade_and_downgrade_when_row_is_deleting() -> None:
    # Given SQLite's existing project constraint (full history is PostgreSQL-specific).
    script = ScriptDirectory(str(ROOT / "apps/api/migrations")).get_revision("051")
    assert script is not None
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("""
            CREATE TABLE creator_projects (
                id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                CONSTRAINT ck_creator_projects_status CHECK (status IN ('draft','active','completed','archived'))
            )
        """))
        connection.execute(sa.text("INSERT INTO creator_projects VALUES (1, 'draft')"))
        # When applying and reverting 051 with an in-flight deletion row.
        with Operations.context(MigrationContext.configure(connection)):
            script.module.upgrade()
            connection.execute(sa.text("UPDATE creator_projects SET status='deleting' WHERE id=1"))
            script.module.downgrade()
        # Then SQLite also preserves the row while restoring the old constraint.
        assert connection.scalar(sa.text("SELECT status FROM creator_projects")) == "archived"
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("UPDATE creator_projects SET status='deleting'"))
    engine.dispose()
