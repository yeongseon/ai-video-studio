import logging
from pathlib import Path

import pytest
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)
from creator_service.object_storage import StorageDeletionError


async def _no_artifacts(_query: str, *_args: object) -> list[dict[str, object]]:
    return []


async def _execute_ok(_query: str, *_args: object) -> str:
    return "DELETE 0"


def _configure_local_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    artifact_root: Path,
) -> None:
    class _Backend:
        def delete(self, _key: str) -> bool:
            return True

    monkeypatch.setenv("ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _no_artifacts)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute_ok)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: _Backend(),
    )


@pytest.mark.asyncio
async def test_delete_refuses_symlinked_run_directory_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    artifact_root.mkdir()
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep")
    (artifact_root / "71").symlink_to(outside, target_is_directory=True)
    _configure_local_cleanup(monkeypatch, artifact_root)

    result = await ArtifactDownloadService(
        InMemoryArtifactDownloadStorage(),
    ).delete_artifacts_for_run(71)

    assert result == 1
    assert protected.read_text() == "keep"
    assert (artifact_root / "71").is_symlink()


@pytest.mark.asyncio
async def test_delete_unlinks_nested_symlinks_without_touching_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    run_dir = artifact_root / "72"
    nested = run_dir / "nested"
    outside = tmp_path / "outside"
    nested.mkdir(parents=True)
    outside.mkdir()
    outside_file = outside / "protected.txt"
    outside_file.write_text("keep")
    outside_dir = outside / "directory"
    outside_dir.mkdir()
    (outside_dir / "child.txt").write_text("keep")
    (nested / "file-link").symlink_to(outside_file)
    (nested / "dir-link").symlink_to(outside_dir, target_is_directory=True)
    _configure_local_cleanup(monkeypatch, artifact_root)

    result = await ArtifactDownloadService(
        InMemoryArtifactDownloadStorage(),
    ).delete_artifacts_for_run(72)

    assert result == 0
    assert not run_dir.exists()
    assert outside_file.read_text() == "keep"
    assert (outside_dir / "child.txt").read_text() == "keep"


@pytest.mark.asyncio
async def test_delete_failure_metadata_and_logs_omit_raw_storage_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sentinel = "customer-private/sentinel-object-name.png"
    execute_calls: list[tuple[object, ...]] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            raise StorageDeletionError(key=key)

    async def fetch_artifact(_query: str, *_args: object) -> list[dict[str, object]]:
        return [{"id": 81, "storage_key": sentinel, "object_storage_provider": "s3"}]

    async def capture_execute(query: str, *args: object) -> str:
        execute_calls.append((query, *args))
        return "UPDATE 1"

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", fetch_artifact)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", capture_execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: _Backend(),
    )
    caplog.set_level(logging.ERROR)

    result = await ArtifactDownloadService(
        InMemoryArtifactDownloadStorage(),
    ).delete_artifacts_for_run(81)

    assert result == 1
    assert sentinel not in caplog.text
    assert sentinel not in repr(execute_calls)
    assert "provider=s3" in repr(execute_calls)
    assert "fingerprint=" in repr(execute_calls)


def test_storage_deletion_error_omits_raw_key() -> None:
    sentinel = "customer-private/sentinel-object-name.png"

    message = str(StorageDeletionError(key=sentinel))

    assert sentinel not in message
    assert "fingerprint=" in message
