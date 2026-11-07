"""Deletion must cross the real PostgreSQL CHECK, not an in-memory substitute."""

from pathlib import Path

import httpx
import pytest
from creator_service.project_service import ProjectService
from creator_service.postgres_project_storage import PostgresProjectStorage
from creator_service.object_storage import LocalStorageBackend

from project_deletion_support import DeletionDatabase, deletion_database, deletion_client


@pytest.mark.asyncio
async def test_mark_deleting_persists_barrier_when_postgres_enforces_checks(
    deletion_database: DeletionDatabase,
) -> None:
    # Given a migrated PostgreSQL project.
    service = ProjectService(PostgresProjectStorage())
    # When setting the barrier through the production adapter.
    project = await service.mark_deleting(1, workspace_id=1)
    # Then it is durably visible from an independent connection.
    assert project.status == "deleting"
    async with deletion_database.pool.acquire() as connection:
        assert await connection.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"


@pytest.mark.asyncio
async def test_delete_succeeds_when_project_is_persisted_in_postgres(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
) -> None:
    # Given an owned project in real PostgreSQL.
    # When deleting through the real app and auth boundary.
    response = await deletion_client.delete("/api/creator/projects/1")
    # Then success means the durable row is gone, not merely a successful response.
    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True, "project_id": 1}
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_projects WHERE id=1") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "deletion-foreign-key"}, {"X-API-Key": "invalid"}])
async def test_delete_rejects_auth_bypass_when_caller_is_not_owner(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase, headers: dict[str, str],
) -> None:
    # Given missing, foreign or invalid credentials (override the client's default key).
    request = deletion_client.build_request("DELETE", "/api/creator/projects/1")
    request.headers.pop("X-API-Key", None)
    request.headers.update(headers)
    # When attempting deletion.
    response = await deletion_client.send(request)
    # Then foreign identities receive anti-enumeration 404; unauthenticated requests receive 401.
    assert response.status_code == (404 if headers.get("X-API-Key") == "deletion-foreign-key" else 401)
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "draft"


class FailingDeleteBackend(LocalStorageBackend):
    def delete(self, key: str) -> bool:
        raise OSError("private-provider-error-with-secret")


@pytest.mark.asyncio
async def test_delete_can_retry_when_storage_fails_after_barrier(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given an actual storage object and artifact, with deletion failing once.
    import creator_service.artifact_download_service as downloads
    await deletion_database.pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1);
        INSERT INTO creator_artifacts (run_id, project_id, artifact_type, file_path, storage_key)
            VALUES (1, 1, 'video', '1/video.mp4', '1/video.mp4');
    """)
    backend = LocalStorageBackend(str(tmp_path))
    backend.upload("1/video.mp4", b"video", "video/mp4")
    monkeypatch.setattr(downloads, "get_storage_backend", lambda: FailingDeleteBackend(str(tmp_path)))
    first = await deletion_client.delete("/api/creator/projects/1")
    assert first.status_code == 503, first.text
    assert first.json() == {"detail": "1 artifact(s) could not be deleted from storage; retry later"}
    assert "private-provider-error" not in first.text
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_runs WHERE id=1") == "cancelled"
    assert await deletion_database.pool.fetchval("SELECT delete_retry_count FROM creator_artifacts WHERE run_id=1") == 1
    assert backend.download_bytes("1/video.mp4") == b"video"
    monkeypatch.setattr(downloads, "get_storage_backend", lambda: backend)
    # When retrying with the backend available again.
    retry = await deletion_client.delete("/api/creator/projects/1")
    # Then access remains possible and cleanup completes rather than returning 404.
    assert retry.status_code == 200, retry.text
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_projects WHERE id=1") == 0
    assert not (tmp_path / "1/video.mp4").exists()
