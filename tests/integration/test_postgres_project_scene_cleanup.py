"""Real FK cascades must not erase the only references to stored scene bytes."""

import importlib
import os
from pathlib import Path
from typing import assert_never

import httpx
import pytest
from creator_service.object_storage import LocalStorageBackend, StorageDeletionError

from project_deletion_support import DeletionDatabase, deletion_database, deletion_client


class RemoteStore:
    """In-memory remote-style byte store; failures leave the object intact."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.failing_keys: set[str] = set()

    def delete(self, key: str) -> bool:
        if key in self.failing_keys:
            raise StorageDeletionError(key)
        return self.objects.pop(key, None) is not None


@pytest.fixture(params=["local", "s3"])
def scene_store(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[str, LocalStorageBackend | RemoteStore]:
    provider: str = request.param
    backend = LocalStorageBackend(str(tmp_path)) if provider == "local" else RemoteStore()
    monkeypatch.setenv("STORAGE_BACKEND", provider)
    downloads = importlib.import_module("creator_service.artifact_download_service")
    monkeypatch.setattr(downloads, "get_storage_backend", lambda: backend)
    return provider, backend


async def seed_scenes(database: DeletionDatabase, provider: str) -> None:
    await database.pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1), (2, 2, 2);
    """)
    await database.pool.execute("""
        INSERT INTO creator_scene_assets (run_id, scene_id, asset_path, storage_key, storage_provider)
            VALUES (1, 'first', 'legacy/first.png', 'legacy/first.png', $1),
                   (1, 'second', 'legacy/second.png', 'legacy/second.png', $1),
                   (2, 'foreign', 'legacy/foreign.png', 'legacy/foreign.png', $1);
    """, provider)


def put_scenes(backend: LocalStorageBackend | RemoteStore) -> None:
    for key in ("legacy/first.png", "legacy/second.png", "legacy/foreign.png"):
        match backend:
            case LocalStorageBackend():
                backend.upload(key, key.encode(), "image/png")
            case RemoteStore():
                backend.objects[key] = key.encode()
            case unreachable:
                assert_never(unreachable)


def remaining_bytes(backend: LocalStorageBackend | RemoteStore) -> dict[str, bytes]:
    match backend:
        case LocalStorageBackend():
            return {
                key: backend.download_bytes(key)
                for key in ("legacy/first.png", "legacy/second.png", "legacy/foreign.png")
                if backend.exists(key)
            }
        case RemoteStore():
            return dict(backend.objects)
        case unreachable:
            assert_never(unreachable)


@pytest.mark.asyncio
async def test_success_removes_scene_bytes_when_legacy_objects_are_outside_run_directory(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    scene_store: tuple[str, LocalStorageBackend | RemoteStore],
) -> None:
    # Given real scene rows and stored bytes, but no artifact rows.
    provider, backend = scene_store
    await seed_scenes(deletion_database, provider)
    put_scenes(backend)
    # When the owner deletes the project through production auth and cleanup.
    response = await deletion_client.delete("/api/creator/projects/1")
    # Then only the foreign project's bytes and rows remain.
    assert response.status_code == 200, response.text
    assert remaining_bytes(backend) == {"legacy/foreign.png": b"legacy/foreign.png"}
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_scene_assets WHERE run_id=1") == 0


@pytest.mark.asyncio
async def test_partial_failure_keeps_scene_references_when_owner_retries(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    scene_store: tuple[str, LocalStorageBackend | RemoteStore], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given one of two owned scene objects cannot be deleted.
    provider, backend = scene_store
    await seed_scenes(deletion_database, provider)
    put_scenes(backend)
    original_delete = backend.delete

    def fail_second(key: str) -> bool:
        if key == "legacy/second.png":
            raise StorageDeletionError(key)
        return original_delete(key)

    monkeypatch.setattr(backend, "delete", fail_second)
    first = await deletion_client.delete("/api/creator/projects/1")
    assert first.status_code == 503, first.text
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_scene_assets WHERE run_id=1") == 2
    assert remaining_bytes(backend) == {
        "legacy/second.png": b"legacy/second.png", "legacy/foreign.png": b"legacy/foreign.png",
    }
    monkeypatch.setattr(backend, "delete", original_delete)
    # When retrying with the first object already missing and the backend recovered.
    retry = await deletion_client.delete("/api/creator/projects/1")
    # Then idempotent cleanup completes and foreign objects remain intact.
    assert retry.status_code == 200, retry.text
    assert remaining_bytes(backend) == {"legacy/foreign.png": b"legacy/foreign.png"}


@pytest.mark.asyncio
async def test_unlink_failure_retains_retry_path_when_legacy_file_has_no_storage_key(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    # Given a keyless legacy scene file and a failing local directory unlink.
    await deletion_database.pool.execute("""
        INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1);
        INSERT INTO creator_scene_assets (run_id, scene_id, asset_path) VALUES (1, 'legacy', '1/scene.png');
    """)
    backend = LocalStorageBackend(str(tmp_path))
    backend.upload("1/scene.png", b"scene", "image/png")
    backend.upload("1/untracked.tmp", b"scratch")
    downloads = importlib.import_module("creator_service.artifact_download_service")
    monkeypatch.setattr(downloads, "get_storage_backend", lambda: backend)
    original_unlink = os.unlink

    def fail_untracked(path: str, *, dir_fd: int | None = None) -> None:
        if path == "untracked.tmp":
            raise PermissionError("private filesystem detail")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", fail_untracked)
    first = await deletion_client.delete("/api/creator/projects/1")
    assert first.status_code == 503, first.text
    assert "private filesystem" not in first.text
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "deleting"
    assert await deletion_database.pool.fetchval("SELECT count(*) FROM creator_scene_assets WHERE run_id=1") == 1
    assert backend.download_bytes("1/untracked.tmp") == b"scratch"
    monkeypatch.setattr(os, "unlink", original_unlink)
    # When local permissions recover and the owner retries.
    retry = await deletion_client.delete("/api/creator/projects/1")
    # Then both the cascade and directory cleanup finish.
    assert retry.status_code == 200, retry.text
    assert not (tmp_path / "1").exists()


@pytest.mark.asyncio
async def test_symlinked_run_root_fails_closed_without_deleting_outside(
    deletion_client: httpx.AsyncClient,
    deletion_database: DeletionDatabase,
    tmp_path: Path,
) -> None:
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1)",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep")
    (tmp_path / "1").symlink_to(outside, target_is_directory=True)

    response = await deletion_client.delete("/api/creator/projects/1")

    assert response.status_code == 503, response.text
    assert protected.read_text() == "keep"
    assert await deletion_database.pool.fetchval(
        "SELECT status FROM creator_projects WHERE id=1",
    ) == "deleting"


@pytest.mark.asyncio
async def test_nested_symlinks_are_unlinked_without_deleting_targets(
    deletion_client: httpx.AsyncClient,
    deletion_database: DeletionDatabase,
    tmp_path: Path,
) -> None:
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1)",
    )
    run_dir = tmp_path / "1"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "protected.txt"
    protected.write_text("keep")
    (run_dir / "file-link").symlink_to(protected)
    (run_dir / "dir-link").symlink_to(outside, target_is_directory=True)

    response = await deletion_client.delete("/api/creator/projects/1")

    assert response.status_code == 200, response.text
    assert protected.read_text() == "keep"
    assert not run_dir.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", [None, "invalid", "deletion-foreign-key"])
async def test_scene_bytes_are_untouched_when_authentication_or_ownership_fails(
    deletion_client: httpx.AsyncClient, deletion_database: DeletionDatabase,
    scene_store: tuple[str, LocalStorageBackend | RemoteStore], credentials: str | None,
) -> None:
    # Given stored scenes in an owned project and missing, invalid or foreign credentials.
    provider, backend = scene_store
    await seed_scenes(deletion_database, provider)
    put_scenes(backend)
    request = deletion_client.build_request("DELETE", "/api/creator/projects/1")
    request.headers.pop("X-API-Key", None)
    if credentials:
        request.headers["X-API-Key"] = credentials
    # When attempting to bypass the authorization boundary.
    response = await deletion_client.send(request)
    # Then unauthorized callers cannot enumerate projects or delete scene bytes.
    assert response.status_code == (404 if credentials == "deletion-foreign-key" else 401)
    assert len(remaining_bytes(backend)) == 3
    assert await deletion_database.pool.fetchval("SELECT status FROM creator_projects WHERE id=1") == "draft"
