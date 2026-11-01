from pathlib import Path
import importlib

import httpx
import pytest
from creator_service.object_storage import LocalStorageBackend

from project_deletion_support import DeletionDatabase, deletion_database, deletion_client


@pytest.mark.asyncio
async def test_delete_cascades_owned_rows_when_workspace_media_and_billing_are_retained(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a populated FK graph, workspace media and independent accounting ledgers.
    pool = deletion_database.pool
    await pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1), (2, 2, 2);
        INSERT INTO creator_run_tasks (run_id, task_type, celery_task_id, status)
            VALUES (1, 'render', 'owned-done', 'success'), (2, 'render', 'foreign-done', 'success');
        INSERT INTO creator_artifacts (run_id, project_id, artifact_type, file_path, storage_key)
            VALUES (1, 1, 'video', '1/video.mp4', '1/video.mp4'),
                   (2, 2, 'video', '2/video.mp4', '2/video.mp4');
        INSERT INTO creator_timelines (project_id, workspace_id, timeline_id, segments_json)
            VALUES (1, 1, 'owned', '[]'), (2, 2, 'foreign', '[]');
        INSERT INTO creator_media_assets (id, workspace_id, project_id, run_id, media_type, origin, storage_key)
            VALUES (1, 1, 1, 1, 'VIDEO', 'UPLOADED', 'workspace/retained.mp4');
        INSERT INTO usage_events (workspace_id, project_id, run_id, provider, model_key, operation_type)
            VALUES (1, 1, 1, 'offline', 'render', 'render');
        INSERT INTO workspace_quota_reservation_owners
            (owner_id, workspace_id, period_start, operation_type, status)
            VALUES ('task:owned-done', 1, now(), 'render', 'settled');
        INSERT INTO workspace_quota_reservation_attempts (owner_id, attempt_id, estimated_cost_usd)
            VALUES ('task:owned-done', 'attempt', 0.01);
    """)
    backend = LocalStorageBackend(str(tmp_path))
    for key in ("1/video.mp4", "2/video.mp4", "workspace/retained.mp4"):
        backend.upload(key, key.encode(), "video/mp4")
    downloads = importlib.import_module("creator_service.artifact_download_service")
    monkeypatch.setattr(downloads, "get_storage_backend", lambda: backend)
    # When deleting the owned project through production cleanup.
    response = await deletion_client.delete("/api/creator/projects/1")
    # Then project/run children cascade, but workspace media detaches and accounting survives.
    assert response.status_code == 200, response.text
    for table, column in (
        ("creator_projects", "id"), ("creator_runs", "project_id"),
        ("creator_run_tasks", "run_id"), ("creator_artifacts", "run_id"),
        ("creator_timelines", "project_id"),
    ):
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=1") == 0, table
        assert await pool.fetchval(f"SELECT count(*) FROM {table} WHERE {column}=2") == 1, table
    media = await pool.fetchrow("SELECT project_id, run_id FROM creator_media_assets WHERE id=1")
    assert media is not None and tuple(media.values()) == (None, None)
    assert await pool.fetchval("SELECT count(*) FROM usage_events WHERE project_id=1 AND run_id=1") == 1
    assert await pool.fetchval("SELECT status FROM workspace_quota_reservation_owners") == "settled"
    assert await pool.fetchval("SELECT count(*) FROM workspace_quota_reservation_attempts") == 1
    assert not backend.exists("1/video.mp4")
    assert backend.download_bytes("2/video.mp4") == b"2/video.mp4"
    assert backend.download_bytes("workspace/retained.mp4") == b"workspace/retained.mp4"
