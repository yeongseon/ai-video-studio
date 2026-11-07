import anyio
import pytest
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected, VisualAssetService

from project_deletion_support import DeletionDatabase, deletion_database


async def seed_run(database: DeletionDatabase) -> None:
    await database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id, status) VALUES (1, 1, 1, 'running')",
    )


async def create_scene() -> None:
    await VisualAssetService(PostgresVisualAssetStorage()).create_asset(
        1,
        "scene-1",
        "1/scenes/new.png",
        storage_provider="s3",
        storage_key="1/scenes/new.png",
    )


@pytest.mark.asyncio
async def test_acceptance_commits_before_delete_barrier_enumeration(
    deletion_database: DeletionDatabase,
) -> None:
    await seed_run(deletion_database)
    pool = deletion_database.pool
    barrier_done = anyio.Event()

    async def mark_deleting() -> None:
        await pool.execute("UPDATE creator_projects SET status='deleting' WHERE id=1")
        barrier_done.set()

    with anyio.fail_after(10):
        async with pool.acquire() as accepting, anyio.create_task_group() as group:
            async with accepting.transaction():
                await accepting.execute(
                    "SELECT p.id FROM creator_projects p JOIN creator_runs r ON r.project_id=p.id "
                    "WHERE r.id=1 FOR SHARE OF p, r",
                )
                await accepting.execute(
                    "INSERT INTO creator_scene_assets (run_id, scene_id, version, asset_path) "
                    "VALUES (1, 'scene-1', 1, '1/scenes/new.png')",
                )
                blocker = await accepting.fetchval("SELECT pg_backend_pid()")
                async with pool.acquire() as observer:
                    group.start_soon(mark_deleting)
                    while not await observer.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE $1 = ANY(pg_blocking_pids(pid)))",
                        blocker,
                    ):
                        await anyio.lowlevel.checkpoint()
                    assert not barrier_done.is_set()

    assert barrier_done.is_set()
    assert await pool.fetchval("SELECT count(*) FROM creator_scene_assets WHERE run_id=1") == 1


@pytest.mark.asyncio
async def test_delete_barrier_commits_before_acceptance_and_rejects_insert(
    deletion_database: DeletionDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_run(deletion_database)
    pool = deletion_database.pool
    rejected: list[SceneAssetAcceptanceRejected] = []

    async def get_pool():
        return pool

    monkeypatch.setattr(
        "creator_service.postgres_visual_asset_storage.get_pool",
        get_pool,
    )

    async def accept() -> None:
        try:
            await create_scene()
        except SceneAssetAcceptanceRejected as error:
            rejected.append(error)

    with anyio.fail_after(10):
        async with pool.acquire() as deleting, anyio.create_task_group() as group:
            async with deleting.transaction():
                await deleting.execute("UPDATE creator_projects SET status='deleting' WHERE id=1")
                blocker = await deleting.fetchval("SELECT pg_backend_pid()")
                group.start_soon(accept)
                async with pool.acquire() as observer:
                    while not await observer.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE $1 = ANY(pg_blocking_pids(pid)))",
                        blocker,
                    ):
                        await anyio.lowlevel.checkpoint()

    assert len(rejected) == 1
    assert await pool.fetchval("SELECT count(*) FROM creator_scene_assets WHERE run_id=1") == 0
