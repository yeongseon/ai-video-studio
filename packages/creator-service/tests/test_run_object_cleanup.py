"""Fast cleanup proofs with an in-memory row source and real object bytes."""

from pathlib import Path
from typing import TypedDict

import pytest
from creator_service.artifact_download_service import ArtifactDownloadService
from creator_service.object_storage import LocalStorageBackend, StorageDeletionError


class ObjectRow(TypedDict):
    id: int | None
    storage_key: str
    object_storage_provider: str


class RemoteObjects:
    """Mutable object store with idempotent deletes and injectable outages."""

    def __init__(self) -> None:
        self.objects = {"legacy/first.png": b"first", "legacy/second.png": b"second"}
        self.failing_keys: set[str] = set()

    def delete(self, key: str) -> bool:
        if key in self.failing_keys:
            raise StorageDeletionError(key)
        return self.objects.pop(key, None) is not None


@pytest.fixture
def scene_objects(monkeypatch: pytest.MonkeyPatch) -> RemoteObjects:
    backend = RemoteObjects()

    async def fetch_rows(query: str, run_id: int) -> list[ObjectRow]:
        assert run_id == 7
        if "creator_scene_assets" not in query:
            return []
        return [
            {"id": None, "storage_key": key, "object_storage_provider": "s3"}
            for key in ("legacy/first.png", "legacy/second.png")
        ]

    async def execute(query: str, *args: int) -> str:
        return "UPDATE 0"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", fetch_rows)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", execute)
    monkeypatch.setattr("creator_service.artifact_download_service.get_storage_backend", lambda: backend)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    return backend


@pytest.mark.asyncio
async def test_scene_bytes_are_deleted_when_rows_are_not_artifacts(scene_objects: RemoteObjects) -> None:
    # Given scene objects without creator_artifacts rows.
    # When cleaning the run.
    failures = await ArtifactDownloadService().delete_artifacts_for_run(7)
    # Then both stored byte sequences disappear.
    assert failures == 0
    assert scene_objects.objects == {}


@pytest.mark.asyncio
async def test_recorded_provider_is_used_when_configuration_changes(
    scene_objects: RemoteObjects, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given old remote objects and identically named local objects after a configuration change.
    local = LocalStorageBackend(str(tmp_path))
    local.upload("legacy/first.png", b"local must survive")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr("creator_service.artifact_download_service.get_storage_backend", lambda: local)
    monkeypatch.setattr("creator_service.run_object_deletion.S3StorageBackend", lambda: scene_objects)
    # When cleaning the run using its recorded provider.
    failures = await ArtifactDownloadService().delete_artifacts_for_run(7)
    # Then remote bytes disappear without deleting same-key bytes from another backend.
    assert failures == 0
    assert scene_objects.objects == {}
    assert local.download_bytes("legacy/first.png") == b"local must survive"


@pytest.mark.asyncio
async def test_partial_scene_failure_is_retryable_when_other_object_is_already_missing(
    scene_objects: RemoteObjects,
) -> None:
    # Given one object fails while another can be deleted.
    scene_objects.failing_keys.add("legacy/second.png")
    first = await ArtifactDownloadService().delete_artifacts_for_run(7)
    assert first == 1
    assert scene_objects.objects == {"legacy/second.png": b"second"}
    scene_objects.failing_keys.clear()
    # When retrying the same row references, including the already-missing object.
    retry = await ArtifactDownloadService().delete_artifacts_for_run(7)
    # Then missing objects are success and the remaining bytes disappear.
    assert retry == 0
    assert scene_objects.objects == {}


@pytest.mark.asyncio
async def test_local_unlink_failure_is_reported_when_untracked_run_file_remains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a legacy run file not indexed by creator_artifacts.
    backend = LocalStorageBackend(str(tmp_path))
    backend.upload("7/scene.png", b"scene")
    original_unlink = Path.unlink

    def denied_unlink(path: Path, missing_ok: bool = False) -> None:
        raise PermissionError("private filesystem detail")

    async def fetch_rows(query: str, run_id: int) -> list[ObjectRow]:
        return []

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", fetch_rows)
    monkeypatch.setattr("creator_service.artifact_download_service.get_storage_backend", lambda: backend)
    monkeypatch.setattr(Path, "unlink", denied_unlink)
    first = await ArtifactDownloadService().delete_artifacts_for_run(7)
    assert first == 1
    assert backend.download_bytes("7/scene.png") == b"scene"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    # When the filesystem permits deletion again.
    retry = await ArtifactDownloadService().delete_artifacts_for_run(7)
    # Then directory cleanup completes.
    assert retry == 0
    assert not (tmp_path / "7").exists()
