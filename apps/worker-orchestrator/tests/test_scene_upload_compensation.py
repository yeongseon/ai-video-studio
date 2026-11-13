"""Durable compensation after acceptance consumes the upload intent."""

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_service import db
from creator_service.artifact_download_service import ArtifactDownloadService
from creator_service.object_storage import LocalStorageBackend, StorageDeletionError, StorageResult
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    RejectedSceneCleanupService,
    rejected_scene_cleanup_service,
)
from creator_service.visual_asset_service import InMemoryVisualAssetStorage
from tasks import generate_scene_image as images
from tasks.scene_batch_lifecycle import SceneBatch
from tasks.task_runner import TaskContext
from worker_loop import run_in_worker_loop

from .scene_cancellation_support import SceneCase, scene_case as scene_case
from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import runner_case as runner_case
from .test_scene_upload_cancellation import upload_backend as upload_backend


@pytest.fixture
def real_cleanup(
    scene_case: SceneCase,
    terminal_pool: asyncpg.Pool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[list[int]]:
    """Use a real artifact ledger, including consumption by the memory asset adapter."""
    case = scene_case
    if not isinstance(case.assets.storage, PostgresVisualAssetStorage):
        run_in_worker_loop(terminal_pool.execute(
            "INSERT INTO creator_runs (id, project_id, workspace_id, current_stage, status) "
            "VALUES ($1, 1, 1, 'VISUAL_ASSET_GENERATING', 'running')",
            case.runner.run_id,
        ))

        async def consume(record_id: int, key: str) -> bool:
            row = await terminal_pool.fetchrow(
                "DELETE FROM creator_artifacts WHERE id = $1 AND storage_key = $2 "
                "AND metadata_json = $3 RETURNING id",
                record_id, key, SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            )
            return row is not None

        case.assets.storage = InMemoryVisualAssetStorage(upload_intent_claim=consume)

    real = RejectedSceneCleanupService()
    intent_ids: list[int] = []

    async def record_intent(**fields) -> int:
        record_id = await real.record_intent(**fields)
        intent_ids.append(record_id)
        return record_id

    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", record_intent)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record", real.record)
    monkeypatch.setattr(rejected_scene_cleanup_service, "cleanup", real.cleanup)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    case.provider.released.set()

    async def cancel_at_accounting(*_args: object, **_kwargs: object) -> None:
        await case.cancel()
        # Memory run storage is separate from the durable cleanup ledger.
        await terminal_pool.execute(
            "UPDATE creator_runs SET status = 'cancelled' WHERE id = $1",
            case.runner.run_id,
        )

    monkeypatch.setattr(images, "record_provider_call", cancel_at_accounting)
    yield intent_ids


def test_accounting_cancellation_retains_retryable_row_when_storage_delete_fails(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    real_cleanup: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given acceptance consumes a real intent and cancellation occurs during accounting.
    case = scene_case
    original_delete = upload_backend.delete

    def fail_delete(key: str) -> bool:
        raise StorageDeletionError(key=key)

    monkeypatch.setattr(upload_backend, "delete", fail_delete)

    # When the worker compensates and object deletion fails.
    result = case.invoke()

    # Then a fresh retryable row owns both the object and its local source.
    path = case.provider.paths[0]
    key = f"{case.runner.run_id}/scenes/{path.name}"
    rows = run_in_worker_loop(db.fetch_all(
        "SELECT * FROM creator_artifacts WHERE run_id = $1 AND storage_key = $2",
        case.runner.run_id, key,
    ))
    assert len(rows) == 1, "consumed upload intent left no durable compensation row"
    row = rows[0]
    assert row["id"] not in real_cleanup
    assert row["metadata_json"] == REJECTED_SCENE_UPLOAD_CLEANUP_MARKER
    assert row["delete_requested_at"] is not None
    assert row["delete_retry_count"] == 1
    assert row["file_path"] == str(path)
    assert result["status"] == "cancelled"
    assert run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id)) == []

    # The real retry task can finish the interrupted deletion without a reclaim grace.
    monkeypatch.setattr(upload_backend, "delete", original_delete)
    monkeypatch.setenv("STORAGE_BACKEND", row["storage_provider"])
    assert run_in_worker_loop(ArtifactDownloadService().retry_failed_deletions()) == 1
    assert not upload_backend.exists(key)
    assert not path.exists()


def test_discard_without_intent_records_cleanup_when_run_is_cancelled(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    real_cleanup: list[int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given cancellation and an uploaded object whose intent is no longer available.
    case = scene_case
    run_in_worker_loop(case.cancel())
    run_in_worker_loop(db.execute(
        "UPDATE creator_runs SET status = 'cancelled' WHERE id = $1", case.runner.run_id,
    ))
    path = tmp_path / "discard.png"
    path.write_bytes(b"discard")
    key = f"{case.runner.run_id}/scenes/{path.name}"
    uploaded = StorageResult(key, 7, "image/png", "checksum", upload_backend.provider)
    batch = SceneBatch(TaskContext(
        run_id=case.runner.run_id, task_id=case.task_id, run={}, workspace_id=1,
        project_id=1, start_time=datetime.now(timezone.utc),
    ), case.runner.runs)

    def fail_delete(key: str) -> bool:
        raise StorageDeletionError(key=key)

    monkeypatch.setattr(upload_backend, "delete", fail_delete)

    # When discard needs a fresh durable record after cancellation.
    run_in_worker_loop(batch.discard_rejected_upload(str(path), uploaded))

    # Then registration is ungated and cleanup failure remains retryable.
    rows = run_in_worker_loop(db.fetch_all(
        "SELECT * FROM creator_artifacts WHERE run_id = $1 AND storage_key = $2",
        case.runner.run_id, key,
    ))
    assert len(rows) == 1
    assert rows[0]["metadata_json"] == REJECTED_SCENE_UPLOAD_CLEANUP_MARKER
    assert rows[0]["delete_retry_count"] == 1


def test_accounting_compensation_is_durable_before_scene_row_deletion(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    real_cleanup: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a timeout after the scene-row DELETE commits, before object cleanup can run.
    case = scene_case
    original_delete = case.assets.storage.delete_asset

    async def interrupted_delete(asset_id: int) -> bool:
        await original_delete(asset_id)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(case.assets.storage, "delete_asset", interrupted_delete)

    # When accounting cancellation is interrupted during compensation.
    with pytest.raises(SoftTimeLimitExceeded):
        case.invoke()

    # Then a committed rejected row already exists and can be retried immediately.
    path = case.provider.paths[0]
    key = f"{case.runner.run_id}/scenes/{path.name}"
    rows = run_in_worker_loop(db.fetch_all(
        "SELECT * FROM creator_artifacts WHERE run_id = $1 AND storage_key = $2",
        case.runner.run_id, key,
    ))
    assert len(rows) == 1, "scene deletion committed before compensation was durable"
    assert rows[0]["id"] not in real_cleanup
    assert rows[0]["metadata_json"] == REJECTED_SCENE_UPLOAD_CLEANUP_MARKER
    assert rows[0]["delete_requested_at"] is not None
    monkeypatch.setenv("STORAGE_BACKEND", rows[0]["storage_provider"])
    assert run_in_worker_loop(ArtifactDownloadService().retry_failed_deletions()) == 1
    assert not upload_backend.exists(key)
    assert not path.exists()
