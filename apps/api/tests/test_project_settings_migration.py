from pathlib import Path
import os
import subprocess
import sys
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
import sqlalchemy as sa
import asyncpg
import pytest


def test_settings_migration_when_upgraded_and_downgraded_sqlite() -> None:
    # Given the previous schema with an existing project row.
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    revision = scripts.get_revision("051")
    assert revision is not None
    assert revision.down_revision == "050"
    with sa.create_engine("sqlite://").begin() as connection:
        connection.execute(sa.text("CREATE TABLE creator_projects (id INTEGER PRIMARY KEY, title TEXT)"))
        connection.execute(sa.text("INSERT INTO creator_projects VALUES (1, 'Legacy')"))
        # When applying the additive table revision, then legacy rows remain unchanged.
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.upgrade()
        assert connection.execute(sa.text("SELECT title FROM creator_projects")).scalar_one() == "Legacy"
        assert connection.execute(sa.text("SELECT COUNT(*) FROM creator_project_settings")).scalar_one() == 0
        column = next(c for c in sa.inspect(connection).get_columns("creator_project_settings") if c["name"] == "settings")
        assert not column["nullable"] and column["default"] is None
        # When rolling back, then only the added table disappears.
        with Operations.context(MigrationContext.configure(connection)):
            revision.module.downgrade()
        assert connection.execute(sa.text("SELECT title FROM creator_projects")).scalar_one() == "Legacy"
        assert "settings" not in {c["name"] for c in sa.inspect(connection).get_columns("creator_projects")}
        assert "creator_project_settings" not in sa.inspect(connection).get_table_names()


@pytest.mark.asyncio
async def test_settings_migration_when_upgraded_and_downgraded_postgres() -> None:
    # Given a schema-isolated database at the previous head with an existing row.
    url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FIRST_SHORT_TEST_DATABASE_URL must name a disposable database")
    schema = f"settings_migration_{uuid.uuid4().hex}"
    root = Path(__file__).resolve().parents[3]
    async with asyncpg.create_pool(url, min_size=1, max_size=2) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'CREATE TABLE "{schema}".alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
        env = {**os.environ, "DATABASE_URL": url, "PGOPTIONS": f"-c search_path={schema}"}
        try:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "050"],
                           cwd=root / "apps/api", env=env, check=True, capture_output=True)
            async with asyncpg.create_pool(url, min_size=1, max_size=2,
                                          server_settings={"search_path": schema}) as pool:
                await pool.execute("INSERT INTO creator_projects (id, title) VALUES (1, 'Legacy')")
                # When upgrading, then preserve old data and absent-row legacy semantics.
                subprocess.run([sys.executable, "-m", "alembic", "upgrade", "051"],
                               cwd=root / "apps/api", env=env, check=True, capture_output=True)
                assert await pool.fetchval("SELECT title FROM creator_projects WHERE id=1") == "Legacy"
                assert await pool.fetchval("SELECT COUNT(*) FROM creator_project_settings") == 0
                column = await pool.fetchrow("""
                    SELECT udt_name, is_nullable, column_default FROM information_schema.columns
                    WHERE table_schema=$1 AND table_name='creator_project_settings' AND column_name='settings'
                """, schema)
                assert column is not None
                assert (column["udt_name"], column["is_nullable"], column["column_default"]) == ("jsonb", "NO", None)
                # When downgrading, then preserve the project and remove only settings.
                subprocess.run([sys.executable, "-m", "alembic", "downgrade", "050"],
                               cwd=root / "apps/api", env=env, check=True, capture_output=True)
                assert await pool.fetchval("SELECT title FROM creator_projects WHERE id=1") == "Legacy"
                assert await pool.fetchval("""
                    SELECT COUNT(*) FROM information_schema.columns
                    WHERE table_schema=$1 AND table_name='creator_project_settings' AND column_name='settings'
                """, schema) == 0
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
