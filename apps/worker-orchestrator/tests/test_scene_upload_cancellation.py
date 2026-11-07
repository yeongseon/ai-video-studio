"""Real scene uploads at the cancellation/asset-acceptance boundary."""

from pathlib import Path
from typing import BinaryIO

import pytest
from creator_service import artifact_storage_integration as integration
from creator_service import object_storage
from creator_service.object_storage import LocalStorageBackend, StorageResult
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected
from tasks.scene_batch_lifecycle import SceneBatch
from tasks import generate_scene_image as images
from worker_loop import run_in_worker_loop

from .scene_cancellation_support import SceneCase, scene_case as scene_case
from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import runner_case as runner_case

REAL_UPLOAD = integration.store_artifact_file


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
