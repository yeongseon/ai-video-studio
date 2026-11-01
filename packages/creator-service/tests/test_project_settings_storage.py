from collections.abc import AsyncIterator
import os
from pathlib import Path
import subprocess
import sys
import uuid

import asyncpg
import pytest

from creator_domain.models.project_settings import ProjectSettings
from creator_service import db
from creator_service.postgres_project_storage import PostgresProjectStorage
from creator_service.project_service import InMemoryProjectStorage, ProjectService


@pytest.fixture(params=["memory", "postgres"])
async def settings_storage(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ProjectService]:
    if request.param == "memory":
        yield ProjectService(InMemoryProjectStorage())
        return
    url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("FIRST_SHORT_TEST_DATABASE_URL must name a disposable database")
    schema = f"settings_{uuid.uuid4().hex}"
    root = Path(__file__).resolve().parents[3]
    async with asyncpg.create_pool(url, min_size=1, max_size=2) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                           cwd=root / "apps/api", check=True, capture_output=True,
                           env={**os.environ, "DATABASE_URL": url, "PGOPTIONS": f"-c search_path={schema}"})
            async with asyncpg.create_pool(url, min_size=1, max_size=2,
                                          server_settings={"search_path": schema}) as pool:
                await pool.execute("""
                    INSERT INTO users (id, auth_subject) VALUES (1, 'settings');
                    INSERT INTO workspaces (id, name, slug, owner_id) VALUES
                        (1, 'Owned', 'owned', 1), (2, 'Foreign', 'foreign', 1);
                """)
                async def get_pool() -> asyncpg.Pool:
                    return pool
                monkeypatch.setattr(db, "get_pool", get_pool)
                yield ProjectService(PostgresProjectStorage())
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.asyncio
async def test_settings_survive_reload_when_created(settings_storage: ProjectService) -> None:
    # Given explicit non-default settings, when creating a persisted project.
    settings = ProjectSettings(output_preset="short_square", target_duration_seconds=240)
    created = await settings_storage.create_project("Square", "idea", idea_brief="idea",
                                                   workspace_id=1, settings=settings)
    restored = await ProjectService(settings_storage.db).get_project(created.id, workspace_id=1)
    # Then settings survive a fresh service instance.
    assert restored is not None
    assert restored.settings == settings


@pytest.mark.asyncio
async def test_settings_update_preserves_row_when_owned(settings_storage: ProjectService) -> None:
    # Given a legacy row, when replacing only settings at its current timestamp.
    created = await settings_storage.create_project("Legacy", "idea", idea_brief="idea", workspace_id=1)
    settings = ProjectSettings(output_preset="short_landscape")
    updated = await settings_storage.replace_settings(created, settings, workspace_id=1)
    # Then status and source survive, timestamp advances, and only settings change.
    assert updated is not None
    assert updated.settings == settings
    assert (updated.title, updated.status, updated.created_at) == (created.title, created.status, created.created_at)
    assert updated.updated_at > created.updated_at


@pytest.mark.asyncio
async def test_settings_update_rejected_when_stale(settings_storage: ProjectService) -> None:
    # Given an access snapshot superseded by a project update.
    created = await settings_storage.create_project("Legacy", "idea", idea_brief="idea", workspace_id=1)
    await settings_storage.update_project(created.id, "Changed", workspace_id=1)
    # When replacing against the old snapshot, then fail the atomic compare-and-swap.
    assert await settings_storage.replace_settings(created, ProjectSettings(), workspace_id=1) is None


@pytest.mark.asyncio
async def test_settings_update_rejected_when_foreign(settings_storage: ProjectService) -> None:
    # Given another workspace's row, when using a mismatched storage scope.
    created = await settings_storage.create_project("Foreign", "idea", idea_brief="idea", workspace_id=2)
    # Then the storage boundary refuses the replacement.
    assert await settings_storage.replace_settings(created, ProjectSettings(), workspace_id=1) is None
