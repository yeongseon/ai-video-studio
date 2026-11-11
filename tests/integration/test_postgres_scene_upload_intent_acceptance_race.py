import asyncio
import contextlib
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import asyncpg
import pytest
from creator_service import db, postgres_visual_asset_storage
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.object_storage import LocalStorageBackend
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

_KEY = "956/scenes/race.png"


class BlockingLocalStorageBackend(LocalStorageBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.delete_entered = Event()
        self.allow_delete = Event()

    def delete(self, key: str) -> bool:
        self.delete_entered.set()
        if not self.allow_delete.wait(timeout=60):
            raise TimeoutError("provider deletion was not released")
        return super().delete(key)


@pytest.fixture(autouse=True)
def acceptance_pool(deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    # asyncpg pools are bound to the loop that created them. save_asset reaches the database
    # through its own get_pool and through db.fetch_one (db.get_pool), so bind both to the
    # fixture pool and run acceptance on this test's loop instead of a foreign thread's loop.
    monkeypatch.setattr(postgres_visual_asset_storage, "get_pool", db.get_pool)


async def _seed_intent(database: DeletionDatabase, local_path: Path) -> None:
    await database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (956, 1, 1)",
    )
    await database.pool.execute(
        """
        INSERT INTO creator_artifacts (
            run_id, workspace_id, project_id, artifact_type, scene_id, file_path,
            storage_key, storage_provider, content_type, metadata_json,
            delete_requested_at, created_at
        ) VALUES (
            956, 1, 1, 'visual_asset', 'race', $1, $2, 'local', 'image/png',
            'scene_upload_cleanup_intent', NOW(), NOW() - INTERVAL '6 minutes'
        )
        """,
        str(local_path),
        _KEY,
    )


async def _asset_row(database: DeletionDatabase) -> dict[str, object]:
    intent_id = await database.pool.fetchval(
        "SELECT id FROM creator_artifacts WHERE run_id = 956 AND storage_key = $1",
        _KEY,
    )
    return {
        "run_id": 956,
        "scene_id": "race",
        "asset_path": "/tmp/race.png",
        "storage_provider": "local",
        "storage_key": _KEY,
        "is_active": True,
        "idempotency_key": "race-acceptance",
        "upload_intent_id": intent_id,
    }


async def _accept(row: dict[str, object]) -> dict[str, object]:
    return await PostgresVisualAssetStorage().save_asset(row)


async def _stop(task: "asyncio.Task[dict[str, object]] | None") -> None:
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _wait_for_blocked_by(
    database: DeletionDatabase, blocker_pid: int, query_fragment: str,
) -> int:
    for _ in range(3_000):
        pid = await database.pool.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE query LIKE $1 "
            "AND wait_event_type = 'Lock' AND $2 = ANY(pg_blocking_pids(pid)) "
            "ORDER BY query_start DESC LIMIT 1",
            f"%{query_fragment}%",
            blocker_pid,
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.01)
    pytest.fail(f"no backend blocked by {blocker_pid} on query fragment {query_fragment!r}")


@pytest.mark.asyncio
async def test_retry_first_rejects_acceptance_without_committing_dangling_scene(
    deletion_database: DeletionDatabase,
    tmp_path: Path,
) -> None:
    # Given retry owns the aged intent and is blocked inside provider deletion.
    backend = BlockingLocalStorageBackend(tmp_path / "objects")
    backend.upload(_KEY, b"race", "image/png")
    local_path = tmp_path / "race.png"
    local_path.write_bytes(b"race-local")
    await _seed_intent(deletion_database, local_path)
    asset_row = await _asset_row(deletion_database)
    retry_pid = 0

    @contextlib.asynccontextmanager
    async def transaction() -> AsyncIterator[asyncpg.Connection]:
        nonlocal retry_pid
        connection = await asyncpg.connect(
            deletion_database.url,
            server_settings={"search_path": deletion_database.schema},
        )
        retry_pid = connection.get_server_pid()
        try:
            async with connection.transaction():
                yield connection
        finally:
            await connection.close()

    async def retry() -> int:
        return await retry_failed_deletions(5, transaction, lambda: backend)

    acceptance: asyncio.Task[dict[str, object]] | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        # The blocking provider delete parks its own thread, so retry runs on a private loop
        # and connection while acceptance runs on this test's loop.
        retry_future = executor.submit(asyncio.run, retry())
        try:
            assert await asyncio.to_thread(backend.delete_entered.wait, 30)
            blocker_pid = retry_pid
            acceptance = asyncio.create_task(_accept(asset_row))
            await _wait_for_blocked_by(deletion_database, blocker_pid, "creator_artifacts")

            # When retry wins ownership and commits deletion.
            backend.allow_delete.set()
            assert await asyncio.wait_for(asyncio.wrap_future(retry_future), timeout=30) == 1
            with pytest.raises(SceneAssetAcceptanceRejected):
                await asyncio.wait_for(acceptance, timeout=30)
        finally:
            backend.allow_delete.set()
            await _stop(acceptance)

    # Then acceptance cannot commit a scene reference to deleted bytes.
    assert not backend.exists(_KEY)
    assert not local_path.exists()
    assert (
        await deletion_database.pool.fetchval(
            "SELECT count(*) FROM creator_scene_assets WHERE run_id = 956 AND storage_key = $1",
            _KEY,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_acceptance_first_keeps_bytes_and_retry_cannot_claim_intent(
    deletion_database: DeletionDatabase,
    tmp_path: Path,
) -> None:
    # Given acceptance owns the intent while it waits on the scene advisory lock, which
    # save_asset takes after claiming the intent and before touching any scene row.
    # A table lock would also stall acceptance's earlier idempotency read and retry's
    # accepted-scene read, so hold only that advisory lock.
    backend = LocalStorageBackend(str(tmp_path / "objects"))
    backend.upload(_KEY, b"race", "image/png")
    local_path = tmp_path / "race.png"
    local_path.write_bytes(b"race-local")
    await _seed_intent(deletion_database, local_path)
    asset_row = await _asset_row(deletion_database)
    blocker = await asyncpg.connect(
        deletion_database.url,
        server_settings={"search_path": deletion_database.schema},
    )
    blocker_transaction = blocker.transaction()
    await blocker_transaction.start()
    await blocker.execute(
        "SELECT pg_advisory_xact_lock($1::int, hashtext($2))",
        asset_row["run_id"],
        asset_row["scene_id"],
    )
    acceptance: asyncio.Task[dict[str, object]] | None = None

    try:
        acceptance = asyncio.create_task(_accept(asset_row))
        await _wait_for_blocked_by(
            deletion_database, blocker.get_server_pid(), "pg_advisory_xact_lock",
        )

        # When retry runs after acceptance owns the intent.
        @contextlib.asynccontextmanager
        async def transaction() -> AsyncIterator[asyncpg.Connection]:
            async with deletion_database.pool.acquire() as connection:
                async with connection.transaction():
                    yield connection

        retry_result = await asyncio.wait_for(
            retry_failed_deletions(5, transaction, lambda: backend), timeout=30,
        )
        await blocker_transaction.rollback()
        accepted = await asyncio.wait_for(acceptance, timeout=30)
    finally:
        if blocker.is_in_transaction():
            await blocker_transaction.rollback()
        await blocker.close()
        await _stop(acceptance)

    # Then acceptance commits atomically and retry never deletes its bytes.
    assert retry_result == 0
    assert accepted["storage_key"] == _KEY
    assert backend.download_bytes(_KEY) == b"race"
    assert local_path.read_bytes() == b"race-local"
    assert (
        await deletion_database.pool.fetchval(
            "SELECT count(*) FROM creator_artifacts WHERE run_id = 956",
        )
        == 0
    )
