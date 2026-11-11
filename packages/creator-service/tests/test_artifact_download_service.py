import asyncio
import contextlib
import os
from pathlib import Path

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)
from creator_service.object_storage import StorageDeletionError
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    RejectedSceneCleanupService,
)


def run(coro):
    return asyncio.run(coro)


def test_get_artifact_by_id_returns_artifact() -> None:
    storage = InMemoryArtifactDownloadStorage()
    service = ArtifactDownloadService(storage)

    created = run(
        storage.save_artifact(
            {
                "run_id": 77,
                "file_path": "77/output.mp4",
                "artifact_type": "video",
                "storage_provider": "local",
            }
        )
    )

    fetched = run(service.get_artifact_by_id(created["id"]))
    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["run_id"] == 77
    assert fetched["file_path"] == "77/output.mp4"


def test_get_artifact_by_id_returns_none_for_missing() -> None:
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    fetched = run(service.get_artifact_by_id(99999))
    assert fetched is None


@pytest.mark.parametrize(
    "marker",
    [SCENE_UPLOAD_CLEANUP_INTENT_MARKER, REJECTED_SCENE_UPLOAD_CLEANUP_MARKER],
)
def test_get_artifact_by_id_hides_internal_cleanup_rows(marker: str) -> None:
    # Given an ordinary artifact and an internal cleanup bookkeeping row in the same run.
    storage = InMemoryArtifactDownloadStorage()
    service = ArtifactDownloadService(storage)
    ordinary = run(
        storage.save_artifact(
            {
                "run_id": 77,
                "file_path": "77/output.mp4",
                "artifact_type": "video",
                "storage_provider": "local",
                "metadata_json": None,
            }
        )
    )
    internal = run(
        storage.save_artifact(
            {
                "run_id": 77,
                "file_path": "/artifacts/77/scenes/pending.png",
                "storage_key": "77/scenes/pending.png",
                "artifact_type": "visual_asset",
                "storage_provider": "local",
                "metadata_json": marker,
            }
        )
    )

    # When either row is requested by id.
    fetched_ordinary = run(service.get_artifact_by_id(ordinary["id"]))
    fetched_internal = run(service.get_artifact_by_id(internal["id"]))

    # Then only the ordinary artifact is addressable; cleanup rows are not artifacts.
    assert fetched_ordinary is not None
    assert fetched_ordinary["id"] == ordinary["id"]
    assert fetched_internal is None


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_deletes_storage_and_db_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_keys: list[str] = []
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            deleted_keys.append(key)
            return True

    async def _fetch_all(_query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in _query:
            return [
                {"id": 11, "storage_key": "77/audio.mp3", "storage_provider": "local"},
                {"id": 12, "storage_key": "77/video.mp4", "storage_provider": "s3"},
            ]
        return []

    async def _execute(_query: str, *args: object) -> str:
        execute_calls.append((_query, *args))
        return "DELETE 2"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(77)

    assert result == 0
    assert deleted_keys == ["77/audio.mp3", "77/video.mp4"]
    delete_calls = [c for c in execute_calls if "DELETE FROM" in c[0]]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == [11, 12]


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_only_deletes_successful_rows_when_some_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_keys: list[str] = []
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            deleted_keys.append(key)
            if key == "88/video.mp4":
                raise RuntimeError("s3 unavailable")
            return True

    async def _fetch_all(_query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in _query:
            return [
                {"id": 21, "storage_key": "88/audio.mp3", "storage_provider": "local"},
                {"id": 22, "storage_key": "88/video.mp4", "storage_provider": "s3"},
            ]
        return []

    async def _execute(_query: str, *args: object) -> str:
        execute_calls.append((_query, *args))
        return "DELETE 1"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(88)

    assert result == 1
    assert deleted_keys == ["88/audio.mp3", "88/video.mp4"]
    delete_calls = [c for c in execute_calls if "DELETE FROM" in c[0]]
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == [21]


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_keeps_all_rows_when_all_deletes_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, _key: str) -> bool:
            raise RuntimeError("storage down")

    async def _fetch_all(_query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in _query:
            return [
                {"id": 31, "storage_key": "89/audio.mp3", "storage_provider": "local"},
                {"id": 32, "storage_key": "89/video.mp4", "storage_provider": "s3"},
            ]
        return []

    async def _execute(_query: str, *args: object) -> str:
        execute_calls.append((_query, *args))
        return "DELETE 0"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(89)

    assert result == 2
    # No DELETE FROM calls (only delete_requested_at and failure metadata updates)
    delete_calls = [c for c in execute_calls if "DELETE FROM" in c[0]]
    assert len(delete_calls) == 0


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_local_backend_cleans_run_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "55"
    run_dir.mkdir(parents=True)
    (run_dir / "orphan.tmp").write_text("x")

    class _Backend:
        provider = "local"

        def delete(self, _key: str) -> bool:
            return True

    async def _fetch_all(_query: str, *args: object) -> list[dict[str, object]]:
        return []

    async def _execute(_query: str, *args: object) -> str:
        return "DELETE 0"

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(55)

    assert result == 0
    assert not run_dir.exists()


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_skips_local_cleanup_when_any_delete_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_dir = tmp_path / "56"
    run_dir.mkdir(parents=True)
    (run_dir / "orphan.tmp").write_text("x")

    class _Backend:
        provider = "local"

        def delete(self, _key: str) -> bool:
            raise RuntimeError("delete failed")

    async def _fetch_all(_query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in _query:
            return [{"id": 41, "storage_key": "56/orphan.tmp", "storage_provider": "local"}]
        return []

    async def _execute(_query: str, *args: object) -> str:
        return "DELETE 0"

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(56)

    assert result == 1
    assert run_dir.exists()



# --- PR 7: Retry metadata TDD tests ---


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_sets_delete_requested_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """delete_artifacts_for_run marks all artifacts with delete_requested_at."""
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            return True

    async def _fetch_all(query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in query:
            return [
                {"id": 51, "storage_key": "99/a.mp3", "storage_provider": "local"},
            ]
        return []

    async def _execute(query: str, *args: object) -> str:
        execute_calls.append((query, *args))
        return "OK"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    await service.delete_artifacts_for_run(99)

    assert any("delete_requested_at" in call[0] for call in execute_calls), (
        f"Expected delete_requested_at UPDATE, got: {execute_calls}"
    )


@pytest.mark.asyncio
async def test_delete_artifacts_for_run_records_failure_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On storage failure, records delete_failed_at, delete_error, increments retry_count."""
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            raise RuntimeError("s3 timeout")

    async def _fetch_all(query: str, *args: object) -> list[dict[str, object]]:
        if "storage_key" in query:
            return [
                {"id": 61, "storage_key": "100/a.mp3", "storage_provider": "s3"},
            ]
        return []

    async def _execute(query: str, *args: object) -> str:
        execute_calls.append((query, *args))
        return "OK"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _fetch_all)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    result = await service.delete_artifacts_for_run(100)

    assert result == 1
    failure_updates = [c for c in execute_calls if "delete_failed_at" in c[0] and "delete_error" in c[0]]
    assert len(failure_updates) == 1, f"Expected 1 failure metadata UPDATE, got: {execute_calls}"


class _FakeConnection:
    """Fake asyncpg connection that records all queries."""

    def __init__(self, fetch_rows: list[dict[str, object]] | None = None) -> None:
        self._fetch_rows = fetch_rows or []
        self.queries: list[tuple[str, ...]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.queries.append((query, *args))
        return self._fetch_rows

    async def execute(self, query: str, *args: object) -> str:
        self.queries.append((query, *args))
        return "OK"

    async def fetchval(self, query: str, *args: object) -> int:
        self.queries.append((query, *args))
        return 0  # no abandoned rows in tests

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.queries.append((query, *args))
        if "FOR UPDATE SKIP LOCKED" in query:
            attempted_ids = args[1]
            assert isinstance(attempted_ids, list)
            return next(
                (row for row in self._fetch_rows if row.get("id") not in attempted_ids),
                None,
            )
        return {"id": 501}

@contextlib.asynccontextmanager
async def _fake_transaction(conn: _FakeConnection):
    yield conn


@pytest.mark.asyncio
async def test_rejected_scene_upload_is_recorded_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection()
    monkeypatch.setattr(
        "creator_service.rejected_scene_cleanup.transaction",
        lambda: _fake_transaction(conn),
    )
    service = RejectedSceneCleanupService()

    record_id = await service.record(
        run_id=17,
        workspace_id=3,
        project_id=9,
        storage_key="17/scenes/scene-a-1234.png",
        local_path="/tmp/17/scenes/scene-a-1234.png",
        storage_provider="s3",
        content_type="image/png",
        size_bytes=12,
        checksum="abc",
    )

    assert record_id == 501
    inserts = [query for query in conn.queries if "INSERT INTO creator_artifacts" in query[0]]
    assert len(inserts) == 1
    assert inserts[0][6] == "17/scenes/scene-a-1234.png"
    assert "delete_requested_at" in inserts[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [StorageDeletionError(key="17/scenes/fresh.png"), RuntimeError("down")])
async def test_rejected_scene_cleanup_failure_stays_retryable_then_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    key = "17/scenes/fresh.png"
    local_path = tmp_path / "fresh.png"
    local_path.write_bytes(b"fresh")
    conn = _FakeConnection(fetch_rows=[{
        "id": 502,
        "storage_key": key,
        "file_path": str(local_path),
        "metadata_json": REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        "delete_retry_count": 0,
    }])
    attempts = 0

    class _Backend:
        def delete(self, candidate: str) -> bool:
            nonlocal attempts
            assert candidate == key
            attempts += 1
            if attempts == 1:
                raise failure
            return True

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend(),
    )
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    assert await service.retry_failed_deletions() == 0
    assert not [query for query in conn.queries if "DELETE FROM creator_artifacts" in query[0]]
    assert local_path.exists()
    assert await service.retry_failed_deletions() == 1
    assert not local_path.exists()
    assert [query for query in conn.queries if "DELETE FROM creator_artifacts" in query[0]]


@pytest.mark.asyncio
async def test_rejected_scene_local_unlink_failure_stays_retryable_then_clears(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    key = "17/scenes/fresh.png"
    local_path = tmp_path / "fresh.png"
    local_path.write_bytes(b"fresh")
    conn = _FakeConnection(fetch_rows=[{
        "id": 503,
        "storage_key": key,
        "file_path": str(local_path),
        "metadata_json": REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        "delete_retry_count": 0,
    }])
    original_unlink = os.unlink
    fail_unlink = True

    class _Backend:
        def delete(self, candidate: str) -> bool:
            assert candidate == key
            return True

    def unlink(path, *, dir_fd=None) -> None:
        if fail_unlink and path == local_path.name:
            raise PermissionError("busy")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend(),
    )
    monkeypatch.setattr(os, "unlink", unlink)
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    assert await service.retry_failed_deletions() == 0
    assert local_path.exists()
    fail_unlink = False
    assert await service.retry_failed_deletions() == 1
    assert not local_path.exists()


@pytest.mark.asyncio
async def test_rejected_scene_soft_timeout_leaves_pending_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    key = "17/scenes/fresh.png"
    local_path = tmp_path / "fresh.png"
    local_path.write_bytes(b"fresh")
    conn = _FakeConnection(fetch_rows=[{
        "id": 504,
        "storage_key": key,
        "file_path": str(local_path),
        "metadata_json": REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        "delete_retry_count": 0,
    }])

    class _Backend:
        def delete(self, candidate: str) -> bool:
            assert candidate == key
            raise SoftTimeLimitExceeded

    monkeypatch.setattr(
        "creator_service.rejected_scene_cleanup.get_storage_backend", lambda: _Backend(),
    )
    service = RejectedSceneCleanupService()

    with pytest.raises(SoftTimeLimitExceeded):
        await service.cleanup(504, key, str(local_path))
    assert not [query for query in conn.queries if "DELETE FROM creator_artifacts" in query[0]]
    assert local_path.exists()


@pytest.mark.asyncio
async def test_retry_soft_timeout_rolls_back_locked_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection(fetch_rows=[{
        "id": 506,
        "storage_key": "17/scenes/fresh.png",
        "file_path": "/tmp/fresh.png",
        "metadata_json": REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        "delete_retry_count": 0,
    }])

    class _Backend:
        def delete(self, key: str) -> bool:
            assert key == "17/scenes/fresh.png"
            raise SoftTimeLimitExceeded

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: _Backend(),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        await ArtifactDownloadService(InMemoryArtifactDownloadStorage()).retry_failed_deletions()

    assert not [query for query in conn.queries if "DELETE FROM creator_artifacts" in query[0]]
    assert not [query for query in conn.queries if "SET delete_failed_at" in query[0]]


@pytest.mark.asyncio
async def test_project_run_deletion_includes_pending_rejected_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    key = "17/scenes/fresh.png"
    local_path = tmp_path / "fresh.png"
    local_path.write_bytes(b"fresh")
    execute_calls: list[tuple[str, ...]] = []

    class _Backend:
        def delete(self, candidate: str) -> bool:
            assert candidate == key
            return True

    async def fetch_rows(query: str, *args: object) -> list[dict[str, object]]:
        assert "metadata_json" in query
        return [{
            "id": 505,
            "storage_key": key,
            "file_path": str(local_path),
            "metadata_json": REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
            "object_storage_provider": "local",
        }]

    async def execute(query: str, *args: object) -> str:
        execute_calls.append((query, *args))
        return "OK"

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", fetch_rows)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend(),
    )
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    assert await service.delete_artifacts_for_run(17) == 0
    assert not local_path.exists()
    assert [query for query in execute_calls if "DELETE FROM creator_artifacts" in query[0]]


@pytest.mark.asyncio
async def test_retry_failed_deletions_claims_and_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_failed_deletions claims rows in a transaction, deletes on success."""
    deleted_keys: list[str] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            deleted_keys.append(key)
            return True

    conn = _FakeConnection(fetch_rows=[
        {"id": 71, "storage_key": "101/a.mp3", "delete_retry_count": 1},
        {"id": 72, "storage_key": "101/b.mp4", "delete_retry_count": 2},
    ])

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    retried = await service.retry_failed_deletions(max_retries=5)

    assert retried == 2
    assert deleted_keys == ["101/a.mp3", "101/b.mp4"]
    delete_calls = [q for q in conn.queries if "DELETE" in q[0]]
    assert [call[1] for call in delete_calls] == [71, 72]


@pytest.mark.asyncio
async def test_retry_failed_deletions_increments_count_on_repeated_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_failed_deletions increments retry_count on repeated failure."""

    class _Backend:
        def delete(self, key: str) -> bool:
            raise RuntimeError("still broken")

    conn = _FakeConnection(fetch_rows=[
        {"id": 81, "storage_key": "102/a.mp3", "delete_retry_count": 2},
    ])

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    retried = await service.retry_failed_deletions(max_retries=5)

    assert retried == 0
    failure_updates = [q for q in conn.queries if "SET delete_failed_at" in q[0]]
    assert len(failure_updates) == 1


@pytest.mark.asyncio
async def test_retry_failed_deletions_returns_zero_when_none_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retry_failed_deletions returns 0 when no failed artifacts exist."""
    conn = _FakeConnection(fetch_rows=[])

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    retried = await service.retry_failed_deletions()

    assert retried == 0


@pytest.mark.asyncio
async def test_retry_without_pending_rows_does_not_initialize_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConnection(fetch_rows=[])

    def fail_if_initialized() -> None:
        raise AssertionError("storage backend initialized without a retry candidate")

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        fail_if_initialized,
    )

    assert await ArtifactDownloadService(InMemoryArtifactDownloadStorage()).retry_failed_deletions() == 0


@pytest.mark.asyncio
async def test_retry_failed_deletions_rejects_invalid_max_retries() -> None:
    """retry_failed_deletions raises ValueError for max_retries < 1."""
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        await service.retry_failed_deletions(max_retries=0)

    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        await service.retry_failed_deletions(max_retries=-1)


@pytest.mark.asyncio
async def test_retry_uses_for_update_skip_locked_in_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry SELECT uses FOR UPDATE SKIP LOCKED inside a transaction."""
    conn = _FakeConnection(fetch_rows=[])

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    await service.retry_failed_deletions()

    assert "FOR UPDATE SKIP LOCKED" in conn.queries[0][0]


@pytest.mark.asyncio
async def test_retry_picks_up_stranded_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stranded rows (delete_requested_at set, no failure) are included in retry."""
    conn = _FakeConnection(fetch_rows=[])

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    await service.retry_failed_deletions()

    assert "delete_requested_at IS NOT NULL" in conn.queries[0][0]


@pytest.mark.asyncio
async def test_retry_all_operations_use_same_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All DB operations in retry_failed_deletions use the transaction connection."""
    deleted_keys: list[str] = []

    class _Backend:
        def delete(self, key: str) -> bool:
            deleted_keys.append(key)
            if key == "200/fail.mp3":
                raise RuntimeError("boom")
            return True

    conn = _FakeConnection(fetch_rows=[
        {"id": 91, "storage_key": "200/ok.mp4", "delete_retry_count": 0},
        {"id": 92, "storage_key": "200/fail.mp3", "delete_retry_count": 1},
    ])

    # Ensure module-level execute/fetch_all are NOT called
    async def _should_not_be_called(*args: object) -> object:
        raise AssertionError("Module-level DB function called instead of transaction connection")

    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", _should_not_be_called)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", _should_not_be_called)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: _fake_transaction(conn),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend", lambda: _Backend()
    )

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    retried = await service.retry_failed_deletions(max_retries=5)

    assert retried == 1
    assert len(conn.queries) == 6


@pytest.mark.asyncio
async def test_sweep_expired_marks_rows_for_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sweep_expired sets delete_requested_at on rows past expires_at (#587)."""
    captured: dict[str, object] = {}

    # db.execute() returns the asyncpg status string (e.g. "UPDATE 7").
    # The previous test invented a fake _Result.statusmessage class that
    # did not match the real contract — that's how #596 slipped through.
    async def _execute(query: str, *args: object) -> str:
        captured["query"] = query
        captured["batch_size"] = args[0] if args else None
        return "UPDATE 7"

    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    marked = await service.sweep_expired(batch_size=500)

    assert marked == 7
    assert "UPDATE creator_artifacts" in str(captured["query"])
    assert "delete_requested_at = NOW()" in str(captured["query"])
    assert "expires_at < NOW()" in str(captured["query"])
    assert "delete_requested_at IS NULL" in str(captured["query"])
    assert captured["batch_size"] == 500


@pytest.mark.asyncio
async def test_sweep_expired_returns_zero_when_nothing_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _execute(query: str, *args: object) -> str:
        return "UPDATE 0"

    monkeypatch.setattr("creator_service.artifact_download_service.execute", _execute)

    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    marked = await service.sweep_expired()

    assert marked == 0


@pytest.mark.asyncio
async def test_sweep_expired_rejects_invalid_batch_size() -> None:
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
    with pytest.raises(ValueError):
        await service.sweep_expired(batch_size=0)
