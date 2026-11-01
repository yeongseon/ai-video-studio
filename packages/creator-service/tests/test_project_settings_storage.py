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
        if os.getenv("GITHUB_ACTIONS") == "true":
            raise RuntimeError("FIRST_SHORT_TEST_DATABASE_URL is required for settings runtime tests in CI")
        pytest.skip("FIRST_SHORT_TEST_DATABASE_URL must name a disposable database")
    schema = f"settings_{uuid.uuid4().hex}"
    root = Path(__file__).resolve().parents[3]
    async with asyncpg.create_pool(url, min_size=1, max_size=2) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        await admin.execute(f'CREATE TABLE "{schema}".alembic_version (version_num VARCHAR(32) PRIMARY KEY)')
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


@pytest.mark.asyncio
async def test_settings_removed_when_project_deleted(settings_storage: ProjectService) -> None:
    # Given an explicitly configured project, when the project is deleted.
    created = await settings_storage.create_project("Explicit", "idea", idea_brief="idea",
                                                   workspace_id=1, settings=ProjectSettings())
    assert await settings_storage.delete_project(created.id, workspace_id=1)
    # Then no orphan settings remain, including unscoped adapter reads.
    assert await settings_storage.db.fetch_settings(created.id, None) is None
    if isinstance(settings_storage.db, PostgresProjectStorage):
        assert await db.fetch_one("SELECT project_id FROM creator_project_settings WHERE project_id=$1", created.id) is None


@pytest.mark.asyncio
async def test_settings_preserved_when_title_changes(settings_storage: ProjectService) -> None:
    # Given non-default settings, when changing an unrelated project field.
    settings = ProjectSettings(output_preset="short_square", encoding_profile="high_quality")
    created = await settings_storage.create_project("Explicit", "idea", idea_brief="idea",
                                                   workspace_id=1, settings=settings)
    updated = await settings_storage.update_project(created.id, "Renamed", workspace_id=1)
    # Then detail and list both preserve settings from the separate storage seam.
    assert updated is not None and updated.settings == settings
    assert (await settings_storage.list_projects(workspace_id=1))[0].settings == settings


@pytest.mark.asyncio
async def test_raw_project_row_when_settings_explicit(settings_storage: ProjectService) -> None:
    # Given persisted explicit settings, when reading the raw project adapter row.
    created = await settings_storage.create_project("Explicit", "idea", idea_brief="idea",
                                                   workspace_id=1, settings=ProjectSettings())
    row = await settings_storage.db.fetch_project(created.id, workspace_id=1)
    # Then the original row model never receives an added settings column.
    assert row is not None and "settings" not in row


@pytest.mark.asyncio
async def test_settings_reads_when_two_workspaces(settings_storage: ProjectService) -> None:
    # Given two projects with distinguishable explicit choices and one legacy row.
    square = ProjectSettings(output_preset="short_square", target_duration_seconds=240)
    landscape = ProjectSettings(output_preset="short_landscape", target_duration_seconds=3600)
    first = await settings_storage.create_project("A", "idea", idea_brief="A", workspace_id=1, settings=square)
    second = await settings_storage.create_project("B", "idea", idea_brief="B", workspace_id=2, settings=landscape)
    legacy = await settings_storage.create_project("Legacy", "idea", idea_brief="L", workspace_id=None)
    # When reading explicit and legacy settings, then derive scope only from their projects.
    assert await settings_storage.db.fetch_settings(first.id, 2) is None
    assert await settings_storage.db.fetch_settings(second.id, 1) is None
    assert await settings_storage.db.fetch_settings(first.id, 1) == square
    assert await settings_storage.db.fetch_settings(second.id, 2) == landscape
    assert await settings_storage.db.fetch_settings(first.id, None) == square
    unowned = await settings_storage.create_project("Unowned", "idea", idea_brief="U", workspace_id=None, settings=landscape)
    assert await settings_storage.db.fetch_settings(unowned.id, None) == landscape
    assert await settings_storage.db.fetch_settings(unowned.id, 1) is None
    assert await settings_storage.db.fetch_settings(legacy.id, None) is None
    from creator_domain.models.project_settings import resolve_effective_settings
    assert resolve_effective_settings(await settings_storage.db.fetch_settings(legacy.id, None)).source == "legacy_default"


@pytest.mark.asyncio
async def test_settings_visibility_when_project_workspace_moves(settings_storage: ProjectService) -> None:
    # Given explicit settings in workspace1, when the existing project ownership setter moves it.
    settings = ProjectSettings(output_preset="short_square")
    created = await settings_storage.create_project("Moving", "idea", idea_brief="idea", workspace_id=1, settings=settings)
    await settings_storage.db.update_project(created.id, {"workspace_id": 2}, workspace_id=1)
    # Then visibility follows the project atomically, without duplicate ownership state.
    assert await settings_storage.db.fetch_settings(created.id, 1) is None
    assert await settings_storage.db.fetch_settings(created.id, 2) == settings


@pytest.mark.asyncio
async def test_settings_schema_when_ownership_derived(settings_storage: ProjectService) -> None:
    # Given the PostgreSQL settings schema, when inspecting its authoritative columns.
    if not isinstance(settings_storage.db, PostgresProjectStorage):
        return
    rows = await db.fetch_all("""SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name='creator_project_settings'""")
    # Then contradictory workspace ownership is unrepresentable.
    assert {row["column_name"] for row in rows} == {"project_id", "settings", "schema_version", "created_at", "updated_at"}


@pytest.mark.asyncio
async def test_settings_when_unrelated_workspace_deleted(settings_storage: ProjectService) -> None:
    # Given workspace1's configured project, when workspace2 is deleted.
    if not isinstance(settings_storage.db, PostgresProjectStorage):
        return
    settings = ProjectSettings(output_preset="short_square")
    created = await settings_storage.create_project("Survives", "idea", idea_brief="idea", workspace_id=1, settings=settings)
    await db.execute("DELETE FROM workspaces WHERE id=2")
    # Then settings survive; owning-project deletion cascades its settings.
    assert await settings_storage.db.fetch_settings(created.id, 1) == settings
    await settings_storage.delete_project(created.id, workspace_id=1)
    await db.execute("DELETE FROM workspaces WHERE id=1")
    assert await db.fetch_one("SELECT project_id FROM creator_project_settings WHERE project_id=$1", created.id) is None
