import asyncio
import contextlib
from collections.abc import AsyncIterator
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from threading import Event

import asyncpg
import pytest
from creator_service import postgres_visual_asset_storage
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
        if not self.allow_delete.wait(timeout=10):
            raise TimeoutError("provider deletion was not released")
        return super().delete(key)


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


def _run_acceptance(
    row: dict[str, object],
    database_url: str,
    schema: str,
) -> dict[str, object]:
    async def save() -> dict[str, object]:
        async with asyncpg.create_pool(
            database_url,
            min_size=1,
            max_size=1,
            server_settings={"search_path": schema},
        ) as pool:
            async def get_pool() -> asyncpg.Pool:
                return pool

            original_get_pool = postgres_visual_asset_storage.get_pool
            postgres_visual_asset_storage.get_pool = get_pool
            try:
                return await PostgresVisualAssetStorage().save_asset(row)
            finally:
                postgres_visual_asset_storage.get_pool = original_get_pool

    return asyncio.run(save())


async def _blocking_pids(database: DeletionDatabase, pid: int) -> list[int]:
    return list(await database.pool.fetchval("SELECT pg_blocking_pids($1)", pid))


async def _wait_until_blocked(database: DeletionDatabase, pid: int) -> None:
    for _ in range(1_000):
        if await _blocking_pids(database, pid):
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        f"backend {pid} did not block; pg_blocking_pids={await _blocking_pids(database, pid)}",
    )


async def _wait_for_query_pid(database: DeletionDatabase, query_fragment: str) -> int:
    for _ in range(1_000):
        pid = await database.pool.fetchval(
            "SELECT pid FROM pg_stat_activity WHERE query LIKE $1 "
            "AND wait_event_type = 'Lock' ORDER BY query_start DESC LIMIT 1",
            f"%{query_fragment}%",
        )
        if pid is not None:
            return int(pid)
        await asyncio.sleep(0.01)
    pytest.fail(f"no blocked backend found for query fragment {query_fragment!r}")


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

    with ThreadPoolExecutor(max_workers=2) as executor:
        retry_future = executor.submit(asyncio.run, retry())
        assert backend.delete_entered.wait(timeout=10)
        acceptance_future: Future[dict[str, object]] = executor.submit(
            _run_acceptance,
            asset_row,
            deletion_database.url,
            deletion_database.schema,
        )
        acceptance_pid = await _wait_for_query_pid(deletion_database, "creator_artifacts")
        assert retry_pid in await _blocking_pids(deletion_database, acceptance_pid)

        # When retry wins ownership and commits deletion.
        backend.allow_delete.set()
        assert retry_future.result(timeout=10) == 1
        with pytest.raises(SceneAssetAcceptanceRejected):
            acceptance_future.result(timeout=10)

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
    # Given acceptance starts while scene insertion is blocked after intent ownership.
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
    await blocker.execute("LOCK TABLE creator_scene_assets IN ACCESS EXCLUSIVE MODE")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            acceptance_future = executor.submit(
                _run_acceptance,
                asset_row,
                deletion_database.url,
                deletion_database.schema,
            )
            acceptance_pid = await _wait_for_query_pid(
                deletion_database,
                "INSERT INTO creator_scene_assets",
            )
            await _wait_until_blocked(deletion_database, acceptance_pid)

            # When retry runs after acceptance owns the intent.
            @contextlib.asynccontextmanager
            async def transaction() -> AsyncIterator[asyncpg.Connection]:
                async with deletion_database.pool.acquire() as connection:
                    async with connection.transaction():
                        yield connection

            retry_result = await retry_failed_deletions(5, transaction, lambda: backend)
            await blocker_transaction.rollback()
            accepted = acceptance_future.result(timeout=10)
    finally:
        if blocker.is_in_transaction():
            await blocker_transaction.rollback()
        await blocker.close()

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
