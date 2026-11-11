"""Real scene uploads at the cancellation/asset-acceptance boundary."""

from pathlib import Path
from datetime import datetime, timezone
from typing import BinaryIO

import pytest
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from creator_service import artifact_storage_integration as integration
from creator_service import object_storage
from creator_service.rejected_scene_cleanup import (
    RejectedSceneCleanupService,
    SceneUploadIntentRejected,
    rejected_scene_cleanup_service,
)
from creator_service.object_storage import LocalStorageBackend, StorageDeletionError, StorageResult
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected
from tasks.scene_batch_lifecycle import SceneBatch
from tasks.task_runner import TaskContext
from tasks import generate_scene_image as images
from worker_loop import run_in_worker_loop

from .scene_cancellation_support import SceneCase, scene_case as scene_case
from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import runner_case as runner_case

REAL_UPLOAD = integration.store_artifact_file


@pytest.fixture(autouse=True)
def rejected_cleanup_records(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    callspec = getattr(request.node, "callspec", None)
    postgres = callspec is not None and callspec.params.get("runner_case") == "postgres"

    async def record(**fields: object) -> int:
        records.append((str(fields["storage_key"]), str(fields["local_path"])))
        return len(records)

    async def record_intent(
        *,
        run_id: int,
        workspace_id: int | None,
        project_id: int | None,
        storage_key: str,
        local_path: str,
        storage_provider: str,
        content_type: str,
    ) -> int:
        records.append((storage_key, local_path))
        if not postgres:
            return len(records)
        # conftest stubs record_intent with a constant id, but PostgresVisualAssetStorage claims
        # the intent row atomically with the asset insert, so PostgreSQL cases need a real row.
        return await RejectedSceneCleanupService.record_intent(
            rejected_scene_cleanup_service,
            run_id=run_id,
            workspace_id=workspace_id,
            project_id=project_id,
            storage_key=storage_key,
            local_path=local_path,
            storage_provider=storage_provider,
            content_type=content_type,
        )

    async def cleanup(_record_id: int, storage_key: str, local_path: str) -> None:
        try:
            object_storage.get_storage_backend().delete(storage_key)
            Path(local_path).unlink(missing_ok=True)
        except Exception:
            return

    monkeypatch.setattr(rejected_scene_cleanup_service, "record", record)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", record_intent)
    monkeypatch.setattr(rejected_scene_cleanup_service, "cleanup", cleanup)
    return records


class RemoteStorage(LocalStorageBackend):
    """Deterministic remote IO boundary: distinct object root, real bytes/deletion."""

    def upload(self, key: str, data: bytes | BinaryIO, content_type: str = "application/octet-stream") -> StorageResult:
        result = super().upload(key, data, content_type)
        result.storage_provider = "s3"
        return result


@pytest.fixture(params=["local", "remote"])
def upload_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> LocalStorageBackend:
    backend = RemoteStorage(str(tmp_path / "objects")) if request.param == "remote" else LocalStorageBackend(str(tmp_path))
    monkeypatch.setattr(object_storage, "_storage_backend", backend)
    monkeypatch.setattr(integration, "_ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(integration, "store_artifact_file", REAL_UPLOAD)
    return backend


@pytest.mark.parametrize("shared_key", [False, True])
def test_cancel_after_upload_removes_only_unaccepted_output(
    scene_case: SceneCase, upload_backend: LocalStorageBackend, monkeypatch: pytest.MonkeyPatch,
    shared_key: bool,
) -> None:
    # Given a successful real upload, a prior accepted key and cancellation before save.
    case = scene_case
    case.provider.released.set()
    prior_key = f"{case.runner.run_id}/scenes/prior.png"
    upload_backend.upload(prior_key, b"accepted")
    prior = run_in_worker_loop(case.assets.create_asset(
        case.runner.run_id, "scene-0", "prior.png", storage_key=prior_key,
    ))
    uploaded: list[StorageResult] = []
    original = SceneBatch.checkpoint

    def upload(run_id: int, path: str, content_type: str) -> StorageResult:
        result = REAL_UPLOAD(run_id, path, content_type)
        if shared_key:
            result.key = prior_key
        uploaded.append(result)
        return result

    async def checkpoint(batch: SceneBatch) -> None:
        if uploaded:
            await case.cancel()
        await original(batch)

    monkeypatch.setattr(integration, "store_artifact_file", upload)
    monkeypatch.setattr(SceneBatch, "checkpoint", checkpoint)
    # When the actual task observes cancellation at the upload-to-save checkpoint.
    result = case.invoke()
    # Then its orphan is removed locally and remotely; accepted media stays intact.
    assert result["status"] == "cancelled"
    assert case.provider.calls == ["shot-0"]
    if not shared_key:
        assert not upload_backend.exists(uploaded[0].key)
    assert not case.provider.paths[0].exists()
    assert upload_backend.download_bytes(prior_key) == b"accepted"
    assets = run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id))
    assert [asset["id"] for asset in assets] == [prior.id]


@pytest.mark.parametrize("save_outcome", ["accepted", "committed_then_error", "error_before_commit"])
def test_save_boundary_preserves_media_when_commit_may_be_accepted(
    scene_case: SceneCase, upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch, save_outcome: str,
) -> None:
    # Given real persistence, including an ambiguous lost commit acknowledgement.
    case = scene_case
    case.provider.released.set()
    original = case.assets.storage.save_asset

    async def save(row):
        if save_outcome == "error_before_commit":
            await case.cancel()
            raise ConnectionError("save unavailable")
        saved = await original(row)
        await case.cancel()
        if save_outcome == "committed_then_error":
            raise ConnectionError("commit acknowledgement lost")
        return saved

    monkeypatch.setattr(case.assets.storage, "save_asset", save)
    # When cancellation races with the real asset transaction.
    result = case.invoke()
    # Then no potentially DB-accepted media is deleted, including ambiguous failures.
    assert result["status"] == "cancelled"
    assert case.provider.calls == ["shot-0"]
    path = case.provider.paths[0]
    key = f"{case.runner.run_id}/scenes/{path.name}"
    assert path.read_bytes() == b"generated-image"
    assert upload_backend.download_bytes(key) == b"generated-image"
    assets = run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id))
    assert len(assets) == (0 if save_outcome == "error_before_commit" else 1)
    assert result["succeeded"] == (1 if save_outcome == "accepted" else 0)


@pytest.mark.parametrize("shared_key", [False, True])
def test_rejected_acceptance_removes_only_fresh_batch_owned_upload(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    shared_key: bool,
) -> None:
    case = scene_case
    case.provider.released.set()
    prior_key = f"{case.runner.run_id}/scenes/prior.png"
    upload_backend.upload(prior_key, b"accepted")
    uploaded: list[StorageResult] = []
    usage_calls: list[int] = []

    def upload(run_id: int, path: str, content_type: str) -> StorageResult:
        result = REAL_UPLOAD(run_id, path, content_type)
        if shared_key:
            result.key = prior_key
        uploaded.append(result)
        return result

    async def reject(_row: dict[str, object]) -> dict[str, object]:
        raise SceneAssetAcceptanceRejected(run_id=case.runner.run_id)

    async def usage(*_args: object, **_kwargs: object) -> None:
        usage_calls.append(1)

    monkeypatch.setattr(integration, "store_artifact_file", upload)
    monkeypatch.setattr(case.assets.storage, "save_asset", reject)
    monkeypatch.setattr(images, "record_provider_call", usage)

    result = case.invoke()

    assert result["status"] == "cancelled"
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert usage_calls == []
    if not shared_key:
        assert not upload_backend.exists(uploaded[0].key)
    assert upload_backend.download_bytes(prior_key) == b"accepted"
    assets = run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id))
    assert assets == []


@pytest.mark.parametrize("failure", [StorageDeletionError(key="fresh"), RuntimeError("down")])
def test_rejected_acceptance_records_exact_key_before_failed_cleanup(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    rejected_cleanup_records: list[tuple[str, str]],
) -> None:
    case = scene_case
    case.provider.released.set()
    async def reject(_row: dict[str, object]) -> dict[str, object]:
        raise SceneAssetAcceptanceRejected(run_id=case.runner.run_id)

    def fail_delete(_key: str) -> bool:
        raise failure

    monkeypatch.setattr(case.assets.storage, "save_asset", reject)
    monkeypatch.setattr(upload_backend, "delete", fail_delete)

    result = case.invoke()

    expected_key = f"{case.runner.run_id}/scenes/{case.provider.paths[0].name}"
    assert result["status"] == "cancelled"
    assert rejected_cleanup_records == [(expected_key, str(case.provider.paths[0]))]
    assert case.provider.paths[0] in case.provider.paths


def test_rejected_acceptance_never_records_alias_key(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    rejected_cleanup_records: list[tuple[str, str]],
) -> None:
    case = scene_case
    case.provider.released.set()
    alias_key = f"{case.runner.run_id}/scenes/shared.png"
    upload_backend.upload(alias_key, b"accepted")

    def upload(run_id: int, path: str, content_type: str) -> StorageResult:
        result = REAL_UPLOAD(run_id, path, content_type)
        result.key = alias_key
        return result

    async def reject(_row: dict[str, object]) -> dict[str, object]:
        raise SceneAssetAcceptanceRejected(run_id=case.runner.run_id)

    monkeypatch.setattr(integration, "store_artifact_file", upload)
    monkeypatch.setattr(case.assets.storage, "save_asset", reject)

    result = case.invoke()

    assert result["status"] == "cancelled"
    expected_key = f"{case.runner.run_id}/scenes/{case.provider.paths[0].name}"
    assert rejected_cleanup_records == [(expected_key, str(case.provider.paths[0]))]
    assert upload_backend.download_bytes(alias_key) == b"accepted"


@pytest.mark.parametrize("interrupt", [SoftTimeLimitExceeded(), KeyboardInterrupt()])
def test_rejected_cleanup_interruption_leaves_durable_record(
    scene_case: SceneCase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interrupt: BaseException,
) -> None:
    case = scene_case
    path = tmp_path / "fresh.png"
    path.write_bytes(b"fresh")
    key = f"{case.runner.run_id}/scenes/{path.name}"
    records: list[str] = []

    async def record(**fields: object) -> int:
        records.append(str(fields["storage_key"]))
        return 703

    async def cleanup(_record_id: int, _storage_key: str, _local_path: str) -> None:
        raise interrupt

    monkeypatch.setattr(rejected_scene_cleanup_service, "record", record)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", record)
    monkeypatch.setattr(rejected_scene_cleanup_service, "cleanup", cleanup)
    batch = SceneBatch(
        TaskContext(
            run_id=case.runner.run_id,
            task_id=case.task_id,
            run={},
            workspace_id=1,
            project_id=1,
            start_time=datetime.now(timezone.utc),
        ),
        case.runner.runs,
    )
    batch.local_outputs.add(path)
    uploaded = StorageResult(key, 5, "image/png", "abc", "s3")

    with pytest.raises(type(interrupt)):
        run_in_worker_loop(batch.discard_rejected_upload(str(path), uploaded))

    assert records == [key]
    assert path in batch.local_outputs

    async def retry_cleanup(_record_id: int, storage_key: str, local_path: str) -> None:
        assert storage_key == key
        Path(local_path).unlink(missing_ok=True)

    monkeypatch.setattr(rejected_scene_cleanup_service, "cleanup", retry_cleanup)
    run_in_worker_loop(rejected_scene_cleanup_service.cleanup(703, key, str(path)))
    assert not path.exists()


def test_scene_upload_intent_failure_prevents_storage_io(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given durable intent persistence fails for a fresh scene output.
    case = scene_case
    target = tmp_path / str(case.runner.run_id) / "scenes" / "fresh-intent.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fresh")
    batch = SceneBatch(
        TaskContext(
            run_id=case.runner.run_id,
            task_id=case.task_id,
            run={},
            workspace_id=1,
            project_id=1,
            start_time=datetime.now(timezone.utc),
        ),
        case.runner.runs,
    )
    batch.local_outputs.add(target)
    upload_calls: list[str] = []
    original_upload = upload_backend.upload

    async def checkpoint() -> None:
        return None

    async def fail_intent(**_fields: object) -> int:
        raise ConnectionError("intent insert failed")

    def observe_upload(key: str, data: bytes | BinaryIO, content_type: str) -> StorageResult:
        upload_calls.append(key)
        return original_upload(key, data, content_type)

    monkeypatch.setattr(batch, "checkpoint", checkpoint)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", fail_intent, raising=False)
    monkeypatch.setattr(upload_backend, "upload", observe_upload)

    # When the batch attempts to upload the output.
    with pytest.raises(ConnectionError, match="intent insert failed"):
        run_in_worker_loop(batch.upload(str(target)))

    # Then no external storage upload occurs without the durable intent.
    assert upload_calls == []


def test_scene_upload_refused_registration_cancels_without_storage_io(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given the project is being deleted, so the cleanup intent is refused.
    case = scene_case
    target = tmp_path / str(case.runner.run_id) / "scenes" / "late-intent.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"fresh")
    batch = SceneBatch(
        TaskContext(
            run_id=case.runner.run_id,
            task_id=case.task_id,
            run={},
            workspace_id=1,
            project_id=1,
            start_time=datetime.now(timezone.utc),
        ),
        case.runner.runs,
    )
    batch.local_outputs.add(target)
    upload_calls: list[str] = []
    original_upload = upload_backend.upload

    async def checkpoint() -> None:
        return None

    async def refuse_intent(**_fields: object) -> int:
        raise SceneUploadIntentRejected(case.runner.run_id)

    def observe_upload(key: str, data: bytes | BinaryIO, content_type: str) -> StorageResult:
        upload_calls.append(key)
        return original_upload(key, data, content_type)

    monkeypatch.setattr(batch, "checkpoint", checkpoint)
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", refuse_intent, raising=False)
    monkeypatch.setattr(upload_backend, "upload", observe_upload)

    # When the batch attempts to upload the output.
    with pytest.raises(Ignore):
        run_in_worker_loop(batch.upload(str(target)))

    # Then it cancels quietly and nothing reaches external storage.
    assert upload_calls == []


def test_accounting_cancellation_never_deletes_another_deliverys_winner(
    scene_case: SceneCase,
    upload_backend: LocalStorageBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given another delivery already accepted scene-0 under the same idempotency key.
    case = scene_case
    case.provider.released.set()
    winner_key = f"{case.runner.run_id}/scenes/winner.png"
    upload_backend.upload(winner_key, b"winner")
    winner = run_in_worker_loop(case.assets.create_asset(
        case.runner.run_id,
        "scene-0",
        "winner.png",
        storage_provider=upload_backend.provider,
        storage_key=winner_key,
        idempotency_key=f"{case.task_id}:scene-0",
    ))

    async def cancel_at_accounting(*_args: object, **_kwargs: object) -> None:
        await case.cancel()

    monkeypatch.setattr(images, "record_provider_call", cancel_at_accounting)

    # When this redundant delivery replays the winner and is then cancelled at accounting.
    result = case.invoke()

    # Then the winner it never owned survives with its bytes; only its own upload is discarded.
    assert result["status"] == "cancelled"
    assets = run_in_worker_loop(case.assets.storage.list_assets_by_run(case.runner.run_id))
    assert [(asset["id"], asset["storage_key"]) for asset in assets] == [(winner.id, winner_key)]
    assert upload_backend.download_bytes(winner_key) == b"winner"
    own_key = f"{case.runner.run_id}/scenes/{case.provider.paths[0].name}"
    assert not upload_backend.exists(own_key)
