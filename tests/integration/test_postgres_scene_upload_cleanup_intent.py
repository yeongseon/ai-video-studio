from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import asyncpg
import pytest
import pytest_asyncio
from creator_service import (
    artifact_storage_integration,
    object_storage,
    postgres_visual_asset_storage,
)
from creator_service.artifact_download_service import artifact_download_service
from creator_service.object_storage import LocalStorageBackend, StorageResult
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.rejected_scene_cleanup import rejected_scene_cleanup_service
from tasks.scene_batch_lifecycle import SceneBatch
from tasks.task_runner import TaskContext

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

SCENE_UPLOAD_CLEANUP_INTENT_MARKER = "scene_upload_cleanup_intent"


class RemoteStyleStorage(LocalStorageBackend):
    def upload(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageResult:
        result = super().upload(key, data, content_type)
        result.storage_provider = "s3"
        return result

    @property
    def provider(self) -> str:
        return "s3"


class UnusedRuns:
    storage: None = None


@pytest.fixture(params=["local", "remote"])
def scene_backend(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LocalStorageBackend:
    artifact_root = tmp_path / "artifacts"
    backend = (
        RemoteStyleStorage(str(tmp_path / "objects"))
        if request.param == "remote"
        else LocalStorageBackend(str(artifact_root))
    )
    monkeypatch.setattr(object_storage, "_storage_backend", backend)
    monkeypatch.setattr(artifact_storage_integration, "_ARTIFACT_ROOT", artifact_root)
    return backend


@pytest_asyncio.fixture
async def upload_run(deletion_database: DeletionDatabase) -> AsyncIterator[int]:
    run_id = 956
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES ($1, 1, 1)",
        run_id,
    )
    yield run_id


def _batch(run_id: int) -> SceneBatch:
    return SceneBatch(
        TaskContext(
            run_id=run_id,
            task_id="scene-upload-intent-test",
            run={},
            workspace_id=1,
            project_id=1,
            start_time=datetime.now(timezone.utc),
        ),
        UnusedRuns(),
    )


@pytest.mark.asyncio
async def test_interrupt_after_upload_leaves_exact_intent_for_production_retry(
    deletion_database: DeletionDatabase,
    upload_run: int,
    scene_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a fresh UUID-derived scene key and an unrelated shared object.
    expected_key = f"{upload_run}/scenes/fresh-956.png"
    shared_key = f"{upload_run}/scenes/shared.png"
    scene_backend.upload(shared_key, b"accepted")
    target = artifact_storage_integration._ARTIFACT_ROOT / expected_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fresh")
    batch = _batch(upload_run)
    batch.local_outputs.add(target)
    checkpoints = 0

    async def interrupt_after_upload() -> None:
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(batch, "checkpoint", interrupt_after_upload)

    # When the worker is interrupted immediately after storage returns.
    with pytest.raises(KeyboardInterrupt):
        await batch.upload(str(target))

    # Then the exact fresh key is durable, and the production retry removes only it.
    row = await deletion_database.pool.fetchrow(
        "SELECT storage_key, metadata_json FROM creator_artifacts WHERE run_id = $1",
        upload_run,
    )
    assert dict(row) == {
        "storage_key": expected_key,
        "metadata_json": SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    }
    assert scene_backend.download_bytes(expected_key) == b"fresh"
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE run_id = $1",
        upload_run,
    )
    assert await artifact_download_service.retry_failed_deletions() == 1
    assert not scene_backend.exists(expected_key)
    assert scene_backend.download_bytes(shared_key) == b"accepted"


@pytest.mark.asyncio
async def test_intent_insert_failure_prevents_external_upload(
    upload_run: int,
    scene_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given persistence fails before a fresh scene upload can begin.
    expected_key = f"{upload_run}/scenes/fresh-956.png"
    target = artifact_storage_integration._ARTIFACT_ROOT / expected_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fresh")
    batch = _batch(upload_run)
    batch.local_outputs.add(target)
    upload_calls: list[str] = []
    original_upload = scene_backend.upload

    async def fail_intent(**_fields: str | int | None) -> int:
        raise asyncpg.PostgresError("intent insert failed")

    def observe_upload(key: str, data: bytes | BinaryIO, content_type: str) -> StorageResult:
        upload_calls.append(key)
        return original_upload(key, data, content_type)

    monkeypatch.setattr(batch, "checkpoint", _passing_checkpoint)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", fail_intent)
    monkeypatch.setattr(scene_backend, "upload", observe_upload)

    # When upload is requested, the insert error propagates before provider I/O.
    with pytest.raises(asyncpg.PostgresError, match="intent insert failed"):
        await batch.upload(str(target))

    assert upload_calls == []


async def _passing_checkpoint() -> None:
    return None


async def _pool(database: DeletionDatabase) -> asyncpg.Pool:
    return database.pool


@pytest.mark.asyncio
async def test_asset_acceptance_atomically_clears_upload_intent(
    deletion_database: DeletionDatabase,
    upload_run: int,
    scene_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an uploaded scene with a durable cleanup intent.
    expected_key = f"{upload_run}/scenes/accepted-956.png"
    target = artifact_storage_integration._ARTIFACT_ROOT / expected_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"accepted")
    batch = _batch(upload_run)
    batch.local_outputs.add(target)
    monkeypatch.setattr(batch, "checkpoint", _passing_checkpoint)
    monkeypatch.setattr(postgres_visual_asset_storage, "get_pool", lambda: _pool(deletion_database))
    uploaded = await batch.upload(str(target))

    # When the real PostgreSQL acceptance transaction commits the scene asset.
    await PostgresVisualAssetStorage().save_asset({
        "run_id": upload_run,
        "scene_id": "scene-956",
        "asset_path": str(target),
        "storage_provider": uploaded.storage_provider,
        "storage_key": uploaded.key,
        "is_active": True,
        "idempotency_key": "scene-upload-intent-test:scene-956",
    })

    # Then no cleanup intent remains to race with the accepted object.
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_artifacts WHERE run_id = $1",
        upload_run,
    ) == 0
    assert scene_backend.download_bytes(expected_key) == b"accepted"


@pytest.mark.asyncio
async def test_adapter_alias_is_never_recorded_or_deleted(
    deletion_database: DeletionDatabase,
    upload_run: int,
    scene_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an adapter returns a shared alias instead of the requested fresh key.
    expected_key = f"{upload_run}/scenes/fresh-956.png"
    alias_key = f"{upload_run}/scenes/shared.png"
    scene_backend.upload(alias_key, b"accepted")
    target = artifact_storage_integration._ARTIFACT_ROOT / expected_key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fresh")
    batch = _batch(upload_run)
    batch.local_outputs.add(target)
    original_upload = scene_backend.upload

    def aliased_upload(key: str, data: bytes | BinaryIO, content_type: str) -> StorageResult:
        uploaded = original_upload(key, data, content_type)
        uploaded.key = alias_key
        return uploaded

    monkeypatch.setattr(batch, "checkpoint", _passing_checkpoint)
    monkeypatch.setattr(scene_backend, "upload", aliased_upload)

    # When upload completes with the mismatched alias.
    uploaded = await batch.upload(str(target))

    # Then only the exact expected key remains retryable; the shared alias is untouched.
    assert uploaded.key == alias_key
    assert await deletion_database.pool.fetchval(
        "SELECT storage_key FROM creator_artifacts WHERE run_id = $1",
        upload_run,
    ) == expected_key
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE run_id = $1",
        upload_run,
    )
    assert await artifact_download_service.retry_failed_deletions() == 1
    assert not scene_backend.exists(expected_key)
    assert scene_backend.download_bytes(alias_key) == b"accepted"
