"""Real PostgreSQL proofs of candidate eligibility and durable reclamation."""

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.db import transaction
from creator_service.object_storage import LocalStorageBackend
from creator_service.rejected_scene_cleanup import (
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    SCENE_UPLOAD_LATE_WATCH_MARKER,
)

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)
_KEY = "956/scenes/object.png"


async def seed(database: DeletionDatabase, path: Path, metadata: str | None) -> int:
    await database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (956, 1, 1)",
    )
    return await database.pool.fetchval(
        "INSERT INTO creator_artifacts (run_id, workspace_id, project_id, artifact_type, scene_id, "
        "file_path, storage_key, storage_provider, metadata_json, delete_requested_at) "
        "VALUES (956, 1, 1, 'visual_asset', 'object', $1, $2, 'local', $3, NOW()) RETURNING id",
        str(path), _KEY, metadata,
    )


async def assert_retained_as_watch(database: DeletionDatabase, artifact_id: int) -> None:
    row = await database.pool.fetchrow(
        "SELECT delete_failed_at, delete_error FROM creator_artifacts WHERE id = $1", artifact_id,
    )
    assert row is not None, "reclaim erased the only record of a possibly in-flight upload"
    assert row["delete_failed_at"] is not None
    assert row["delete_error"] == SCENE_UPLOAD_LATE_WATCH_MARKER


@pytest.fixture
def media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalStorageBackend, Path]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    backend = LocalStorageBackend(str(tmp_path / "objects"))
    backend.upload(_KEY, b"object")
    path = tmp_path / "source.png"
    path.write_bytes(b"source")
    return backend, path


@pytest.mark.parametrize("metadata", [None, "ordinary", SCENE_UPLOAD_CLEANUP_INTENT_MARKER])
async def test_fresh_candidate_eligibility(
    deletion_database: DeletionDatabase, media: tuple[LocalStorageBackend, Path], metadata: str | None,
) -> None:
    # Given a newly created pending ordinary artifact or unfenced intent.
    backend, path = media
    artifact_id = await seed(deletion_database, path, metadata)
    # When the first retry pass selects candidates.
    result = await retry_failed_deletions(5, transaction, lambda: backend)
    # Then only unfenced intents receive the grace period.
    expected = 0 if metadata == SCENE_UPLOAD_CLEANUP_INTENT_MARKER else 1
    assert result == expected, "ordinary NULL metadata must not inherit intent grace"
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_artifacts WHERE id = $1", artifact_id,
    ) == 1 - expected
    assert backend.exists(_KEY) == (expected == 0)


async def test_aged_intent_is_reclaimed(
    deletion_database: DeletionDatabase, media: tuple[LocalStorageBackend, Path],
) -> None:
    # Given an unfenced intent beyond the grace period.
    backend, path = media
    artifact_id = await seed(deletion_database, path, SCENE_UPLOAD_CLEANUP_INTENT_MARKER)
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE id = $1",
        artifact_id,
    )
    # When retry reclaims it.
    result = await retry_failed_deletions(5, transaction, lambda: backend)
    # Then both forms of bytes are gone and the intent stays behind as a late-write watch.
    assert result == 1
    assert not backend.exists(_KEY)
    assert not path.exists()
    await assert_retained_as_watch(deletion_database, artifact_id)


async def test_ambiguous_delete_keeps_fence_and_resumes_without_grace(
    deletion_database: DeletionDatabase, media: tuple[LocalStorageBackend, Path],
) -> None:
    # Given a delete that removes bytes and then loses its acknowledgement.
    backend, path = media
    artifact_id = await seed(deletion_database, path, SCENE_UPLOAD_CLEANUP_INTENT_MARKER)
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE id = $1",
        artifact_id,
    )
    committed_transactions = 0
    commits_at_delete: list[int] = []

    class AmbiguousBackend(LocalStorageBackend):
        def delete(self, key: str) -> bool:
            commits_at_delete.append(committed_transactions)
            backend.delete(key)
            raise TimeoutError("delete acknowledgement lost")

    @contextlib.asynccontextmanager
    async def observed_transaction() -> AsyncIterator[asyncpg.Connection]:
        nonlocal committed_transactions
        async with transaction() as conn:
            yield conn
        committed_transactions += 1

    # When the ambiguous attempt fails, then a fresh timestamp retries immediately.
    assert await retry_failed_deletions(5, observed_transaction, AmbiguousBackend) == 0
    row = await deletion_database.pool.fetchrow(
        "SELECT delete_failed_at, delete_retry_count FROM creator_artifacts WHERE id = $1", artifact_id,
    )
    assert row is not None
    assert row["delete_failed_at"] is not None
    assert row["delete_retry_count"] == 1
    assert commits_at_delete == [1], "storage deletion began before T1 committed"
    assert not backend.exists(_KEY)
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() WHERE id = $1", artifact_id,
    )
    assert await retry_failed_deletions(5, transaction, lambda: backend) == 1
    assert not path.exists()
    await assert_retained_as_watch(deletion_database, artifact_id)


async def test_crash_between_transactions_leaves_committed_fence(
    deletion_database: DeletionDatabase, media: tuple[LocalStorageBackend, Path],
) -> None:
    # Given an aged intent and a process crash immediately before T2.
    backend, path = media
    artifact_id = await seed(deletion_database, path, SCENE_UPLOAD_CLEANUP_INTENT_MARKER)
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE id = $1",
        artifact_id,
    )
    entries = 0

    @contextlib.asynccontextmanager
    async def crashing_transaction() -> AsyncIterator[asyncpg.Connection]:
        nonlocal entries
        entries += 1
        if entries == 2:
            raise RuntimeError("simulated crash")
        conn = await asyncpg.connect(
            deletion_database.url, server_settings={"search_path": deletion_database.schema},
        )
        try:
            async with conn.transaction():
                yield conn
        finally:
            await conn.close()

    # When the crash interrupts reclamation before storage I/O.
    with pytest.raises(RuntimeError, match="simulated crash"):
        await retry_failed_deletions(5, crashing_transaction, lambda: backend)
    # Then the committed fence survives while bytes remain available for retry.
    row = await deletion_database.pool.fetchrow(
        "SELECT delete_failed_at, delete_error, delete_retry_count FROM creator_artifacts WHERE id = $1",
        artifact_id,
    )
    assert row is not None, "crash removed the unfenced intent before T2"
    assert row["delete_failed_at"] is not None
    assert row["delete_error"] == "scene_upload_reclaim_started"
    assert row["delete_retry_count"] == 0
    assert backend.exists(_KEY)
    assert path.exists()
    assert await retry_failed_deletions(5, transaction, lambda: backend) == 1
    assert not backend.exists(_KEY)
    assert not path.exists()
    await assert_retained_as_watch(deletion_database, artifact_id)
