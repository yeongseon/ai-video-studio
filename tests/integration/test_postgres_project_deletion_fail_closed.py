import importlib
from pathlib import Path

import anyio
import httpx
import pytest
from creator_service.object_storage import LocalStorageBackend
from creator_service.postgres_project_storage import PostgresProjectStorage
from creator_service.project_service import ProjectService

from project_deletion_support import DeletionDatabase, deletion_database, deletion_client


@pytest.mark.asyncio
async def test_mark_deleting_is_idempotent_when_retrying_owned_project(
    deletion_database: DeletionDatabase,
) -> None:
    # Given an already deleting project.
    service = ProjectService(PostgresProjectStorage())
    await service.mark_deleting(1, workspace_id=1)
    # When marking it again before a cleanup retry.
    result = await service.mark_deleting(1, workspace_id=1)
    # Then retry retains identity and cannot affect another workspace.
    assert result.id == 1 and result.status == "deleting"
    with pytest.raises(ValueError):
        await service.mark_deleting(1, workspace_id=2)
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=2") == "draft"


@pytest.mark.asyncio
async def test_delete_preserves_objects_when_task_revocation_cannot_be_confirmed(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a persisted run/object with unavailable external task revocation.
    await deletion_database.pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1);
        INSERT INTO creator_artifacts (run_id, artifact_type, file_path, storage_key)
            VALUES (1, 'video', '1/video.mp4', '1/video.mp4');
    """)
    backend = LocalStorageBackend(str(tmp_path))
    backend.upload("1/video.mp4", b"keep", "video/mp4")
    utils = importlib.import_module("shorts_api.routes.creator_runs_utils")

    async def unavailable(run_id: int) -> bool:
        return False

    monkeypatch.setattr(utils, "_revoke_active_tasks_for_run", unavailable)
    # When deletion attempts cleanup.
    response = await deletion_client.delete("/api/creator/projects/1")
    # Then fail-closed cancellation precedes object I/O and retains retryable metadata.
    assert response.status_code == 503
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_runs WHERE id=1") == "cancelled"
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_artifacts") == 1
    assert backend.download_bytes("1/video.mp4") == b"keep"


@pytest.mark.asyncio
async def test_delete_retry_survives_when_request_is_cancelled_after_barrier(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a request paused after durable barrier persistence, before artifact deletion.
    entered = anyio.Event()
    core = importlib.import_module("shorts_api.routes.creator_projects")
    original = core._remove_run_artifacts

    async def pause(run_ids: list[int]) -> int:
        entered.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(core, "_remove_run_artifacts", pause)

    async def delete() -> None:
        await deletion_client.delete("/api/creator/projects/1")

    # When the in-flight request is cancelled.
    with anyio.fail_after(10):
        async with anyio.create_task_group() as group:
            group.start_soon(delete)
            await entered.wait()
            group.cancel_scope.cancel()
    # Then the persisted barrier remains and a fresh request can finish.
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"
    monkeypatch.setattr(core, "_remove_run_artifacts", original)
    retry = await deletion_client.delete("/api/creator/projects/1")
    assert retry.status_code == 200, retry.text
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_projects WHERE id=1") == 0
