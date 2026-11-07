from pathlib import Path
import os
import json
import subprocess
import sys
import uuid

import asyncpg
import pytest


@pytest.mark.asyncio
async def test_previous_release_reads_when_settings_schema_is_upgraded() -> None:
    # Given the previous release's actual model and a disposable pre-052 schema.
    url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if not url:
        if os.getenv("GITHUB_ACTIONS") == "true":
            raise RuntimeError("FIRST_SHORT_TEST_DATABASE_URL is required for settings runtime tests in CI")
        pytest.skip("FIRST_SHORT_TEST_DATABASE_URL must name a disposable database")
    root = Path(__file__).resolve().parents[3]
    model_source = subprocess.run(
        ["git", "show", "origin/main:packages/creator-domain/creator_domain/models/project.py"],
        cwd=root, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_MASTER": "1"},
    ).stdout
    schema = f"settings_rollback_{uuid.uuid4().hex}"
    env = {**os.environ, "DATABASE_URL": url, "PGOPTIONS": f"-c search_path={schema}"}
    async with asyncpg.create_pool(url, min_size=1, max_size=2) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'CREATE TABLE "{schema}".alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
        try:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "051"],
                           cwd=root / "apps/api", env=env, check=True, capture_output=True)
            async with asyncpg.create_pool(url, min_size=1, max_size=2,
                                          server_settings={"search_path": schema}, statement_cache_size=0) as pool:
                await pool.execute("INSERT INTO creator_projects (id, title) VALUES (1, 'Legacy'), (2, 'Explicit')")
                before = await pool.fetchrow("SELECT * FROM creator_projects WHERE id = $1", 1)
                assert before is not None
                # When the candidate schema is migrated, then the old raw-row contract is identical.
                subprocess.run([sys.executable, "-m", "alembic", "upgrade", "052"],
                               cwd=root / "apps/api", env=env, check=True)
                after = await pool.fetchrow("SELECT * FROM creator_projects WHERE id = $1", 1)
                assert after is not None
                assert set(after.keys()) == set(before.keys())
                await pool.execute("""
                    INSERT INTO creator_project_settings (project_id, settings)
                    VALUES (2, '{"schema_version":1,"output_preset":"short_square"}'::jsonb)
                """)
                for project_id in (1, 2):
                    row = await pool.fetchrow("SELECT * FROM creator_projects WHERE id = $1", project_id)
                    assert row is not None and set(row.keys()) == set(before.keys())
                    subprocess.run(
                        [sys.executable, "-c", model_source + "\nimport sys\nProject.model_validate_json(sys.stdin.read())\n"],
                        input=json.dumps(dict(row), default=str), text=True,
                        cwd=root, check=True, capture_output=True,
                    )
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
