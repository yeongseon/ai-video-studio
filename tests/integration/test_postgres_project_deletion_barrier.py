import importlib

import anyio
import httpx
import pytest
from creator_service.postgres_project_storage import PostgresProjectStorage
from creator_service.postgres_run_storage import PostgresRunStorage
from creator_service.project_service import ProjectService
from creator_service.run_service import ConflictError, RunService

from project_deletion_support import DeletionDatabase, deletion_database, deletion_client


@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload", [
    ("runs", {"model_defaults": {}, "style_preset": "default"}),
    ("script/import-markdown", {"markdown": "# Scene\nHello"}),
    ("script/import-json", {"json_script": "[]"}),
])
async def test_writers_reject_when_delete_barrier_is_committed(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    path: str, payload: dict[str, str | dict[str, str]],
) -> None:
    # Given the durable barrier committed by another connection.
    await ProjectService(PostgresProjectStorage()).mark_deleting(1, workspace_id=1)
    # When any project entry writer attempts to create a run.
    response = await deletion_client.post(f"/api/creator/projects/1/{path}", json=payload)
    # Then the writer rejects rather than persisting rows.
    assert response.status_code == 409, response.text
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_runs") == 0


@pytest.mark.asyncio
async def test_stale_post_request_rejects_when_barrier_commits_after_access_check(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a request whose project access check observed draft before deletion committed.
    checked = anyio.Event()
    resume = anyio.Event()
    core = importlib.import_module("shorts_api.routes.creator_runs_core")
    service = RunService(PostgresRunStorage())

    class PausedRunService(RunService):
        async def create_run(self, *args, **kwargs):
            checked.set()
            await resume.wait()
            return await service.create_run(*args, **kwargs)

    monkeypatch.setattr(core, "run_service", PausedRunService(PostgresRunStorage()))
    responses: list[httpx.Response] = []

    async def post() -> None:
        responses.append(await deletion_client.post(
            "/api/creator/projects/1/runs", json={"model_defaults": {}, "style_preset": "default"},
        ))

    # When a second connection commits the barrier before the INSERT resumes.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(post)
            await checked.wait()
            await ProjectService(PostgresProjectStorage()).mark_deleting(1, workspace_id=1)
            resume.set()
    # Then PostgreSQL's atomic predicate rejects even though the HTTP snapshot was stale.
    assert responses[0].status_code == 409, responses[0].text
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_runs") == 0


@pytest.mark.asyncio
async def test_adapter_rejects_when_project_has_deleting_status(
    deletion_database: DeletionDatabase,
) -> None:
    # Given the barrier independently of HTTP access and script checks.
    await ProjectService(PostgresProjectStorage()).mark_deleting(1, workspace_id=1)
    # When another writer uses the service directly.
    with pytest.raises(ConflictError):
        await RunService(PostgresRunStorage()).create_run(1, {}, "default", workspace_id=1)
    # Then no run is stored.
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_runs") == 0


@pytest.mark.asyncio
async def test_owner_reads_and_title_update_remain_available_when_deletion_needs_retry(
    deletion_client: httpx.AsyncClient,
) -> None:
    # Given a deleting project: existing reads deliberately have no status filter.
    await ProjectService(PostgresProjectStorage()).mark_deleting(1, workspace_id=1)
    # When the owner reads and renames it, without creating any run.
    detail = await deletion_client.get("/api/creator/projects/1")
    listing = await deletion_client.get("/api/creator/projects")
    updated = await deletion_client.patch("/api/creator/projects/1", json={"title": "Retry pending"})
    # Then the status is still deleting and DELETE's access dependency can locate it on retry.
    assert detail.status_code == updated.status_code == listing.status_code == 200
    assert detail.json()["status"] == updated.json()["status"] == "deleting"
    assert listing.json()["total"] == 1
    assert listing.json()["projects"][0]["status"] == "deleting"
