import asyncio
import contextlib
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import asyncpg
import pytest
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)
from creator_service.object_storage import LocalStorageBackend

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

_KEY = "956/scenes/accepted.png"


async def _seed_accepted_asset_and_stale_intent(
    database: DeletionDatabase,
    local_path: Path,
) -> None:
    await database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (956, 1, 1)",
    )
    await database.pool.execute(
        """
        INSERT INTO creator_scene_assets (
            run_id, scene_id, asset_path, storage_key, storage_provider, is_active
        ) VALUES (956, 'accepted', $1, $2, 'local', true)
        """,
        str(local_path),
        _KEY,
    )
    await database.pool.execute(
        """
        INSERT INTO creator_artifacts (
            run_id, workspace_id, project_id, artifact_type, scene_id, file_path,
            storage_key, storage_provider, content_type, metadata_json, delete_requested_at
        ) VALUES (
            956, 1, 1, 'visual_asset', 'accepted', $1, $2, 'local', 'image/png',
            'scene_upload_cleanup_intent', NOW()
        )
        """,
        str(local_path),
        _KEY,
    )
    await database.pool.execute(
        """
        UPDATE creator_artifacts
        SET created_at = NOW() - INTERVAL '6 minutes'
        WHERE run_id = 956
        """,
    )


@pytest.mark.asyncio
async def test_retry_discards_stale_intent_without_deleting_accepted_media(
    deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given an accepted scene and an aged cleanup intent for its exact object.
    backend = LocalStorageBackend(str(tmp_path / "objects"))
    backend.upload(_KEY, b"accepted", "image/png")
    local_path = tmp_path / "accepted.png"
    local_path.write_bytes(b"accepted-local")
    await _seed_accepted_asset_and_stale_intent(deletion_database, local_path)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: backend,
    )

    # When the production retry processes the stale intent.
    retried = await ArtifactDownloadService(
        InMemoryArtifactDownloadStorage(),
    ).retry_failed_deletions()

    # Then only the stale intent is removed; accepted object references remain valid.
    assert retried == 1
    assert backend.download_bytes(_KEY) == b"accepted"
    assert local_path.read_bytes() == b"accepted-local"
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_scene_assets WHERE run_id = 956 AND storage_key = $1",
        _KEY,
    ) == 1
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_artifacts WHERE run_id = 956",
    ) == 0


@pytest.mark.asyncio
async def test_two_retry_workers_compose_row_lock_with_accepted_reference_guard(
    deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given an accepted object whose reference lookup is temporarily blocked.
    backend = LocalStorageBackend(str(tmp_path / "objects"))
    backend.upload(_KEY, b"accepted", "image/png")
    local_path = tmp_path / "accepted.png"
    local_path.write_bytes(b"accepted-local")
    await _seed_accepted_asset_and_stale_intent(deletion_database, local_path)
    first_backend_pid = 0

    @contextlib.asynccontextmanager
    async def isolated_transaction() -> AsyncIterator[asyncpg.Connection]:
        nonlocal first_backend_pid
        connection = await asyncpg.connect(
            deletion_database.url,
            server_settings={"search_path": deletion_database.schema},
        )
        try:
            async with connection.transaction():
                if first_backend_pid == 0:
                    first_backend_pid = connection.get_server_pid()
                yield connection
        finally:
            await connection.close()

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        isolated_transaction,
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: backend,
    )

    blocker = await asyncpg.connect(
        deletion_database.url,
        server_settings={"search_path": deletion_database.schema},
    )
    blocker_transaction = blocker.transaction()
    await blocker_transaction.start()
    await blocker.execute("LOCK TABLE creator_scene_assets IN ACCESS EXCLUSIVE MODE")

    def retry_once() -> int:
        service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())
        return asyncio.run(service.retry_failed_deletions())

    try:
        # When one worker holds the intent row lock while checking acceptance.
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(retry_once)
            for _ in range(1_000):
                if first_backend_pid > 0 and await deletion_database.pool.fetchval(
                    "SELECT cardinality(pg_blocking_pids($1)) > 0",
                    first_backend_pid,
                ):
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail("first retry worker did not block on the accepted-reference lookup")

            second = executor.submit(retry_once)
            second_result = second.result(timeout=10)
            await blocker_transaction.rollback()
            first_result = first.result(timeout=10)
    finally:
        if blocker.is_in_transaction():
            await blocker_transaction.rollback()
        await blocker.close()

    # Then the contender skips the locked intent and neither worker deletes accepted bytes.
    assert second_result == 0
    assert first_result == 1
    assert backend.download_bytes(_KEY) == b"accepted"
    assert local_path.read_bytes() == b"accepted-local"
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_artifacts WHERE run_id = 956",
    ) == 0
