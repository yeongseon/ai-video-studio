import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from creator_domain.sanitize import sanitize_path_component
from creator_service import db, postgres_visual_asset_storage
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.object_storage import LocalStorageBackend
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.rejected_scene_cleanup import (
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    SceneUploadIntentRejected,
    rejected_scene_cleanup_service,
)
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected
from tasks.hard_timeout import delivery_owner

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

_RUN = 956
_SCENE = "scene-a"
_ASYNC_WAIT_SECONDS = 30


@pytest.fixture(autouse=True)
def acceptance_pool(deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_visual_asset_storage, "get_pool", db.get_pool)


@pytest.fixture
async def run_seeded(deletion_database: DeletionDatabase) -> DeletionDatabase:
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES ($1, 1, 1)", _RUN,
    )
    return deletion_database


async def _seed_intent(
    database: DeletionDatabase,
    key: str,
    *,
    scene: str = _SCENE,
    fenced: bool = False,
    file_path: str | None = None,
) -> int:
    return int(
        await database.pool.fetchval(
            """
            INSERT INTO creator_artifacts (
                run_id, workspace_id, project_id, artifact_type, scene_id, file_path,
                storage_key, storage_provider, content_type, metadata_json,
                delete_requested_at, delete_failed_at, delete_error
            ) VALUES (
                $1, 1, 1, 'visual_asset', $2, COALESCE($6, $3), $3, 'local', 'image/png', $4, NOW(),
                CASE WHEN $5 THEN NOW() END,
                CASE WHEN $5 THEN 'scene_upload_reclaim_started' END
            ) RETURNING id
            """,
            _RUN, scene, key, SCENE_UPLOAD_CLEANUP_INTENT_MARKER, fenced, file_path,
        )
    )


def _row(
    key: str,
    intent_id: int | None,
    *,
    idempotency_key: str = "task-1:scene-a",
    scene: str = _SCENE,
) -> dict[str, object]:
    return {
        "run_id": _RUN,
        "scene_id": scene,
        "asset_path": f"/tmp/{key.rsplit('/', 1)[-1]}",
        "storage_provider": "local",
        "storage_key": key,
        "is_active": True,
        "idempotency_key": idempotency_key,
        "upload_intent_id": intent_id,
    }


async def _accept(row: dict[str, object]) -> dict[str, object]:
    return await PostgresVisualAssetStorage().save_asset(row)


async def _assets(database: DeletionDatabase) -> list[asyncpg.Record]:
    return await database.pool.fetch(
        "SELECT scene_id, storage_key, is_active, idempotency_key FROM creator_scene_assets "
        "WHERE run_id = $1 ORDER BY id",
        _RUN,
    )


async def _intent_ids(database: DeletionDatabase) -> set[int]:
    rows = await database.pool.fetch(
        "SELECT id FROM creator_artifacts WHERE run_id = $1 AND metadata_json = $2",
        _RUN, SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    )
    return {int(row["id"]) for row in rows}


@contextlib.asynccontextmanager
async def _held(
    database: DeletionDatabase, statement: str, *args: object, commit: bool = False,
) -> AsyncIterator[int]:
    connection = await asyncpg.connect(
        database.url, server_settings={"search_path": database.schema},
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(statement, *args)
        yield int(connection.get_server_pid())
        if commit:
            await transaction.commit()
    finally:
        if connection.is_in_transaction():
            await transaction.rollback()
        await connection.close()


async def _blocked_count(database: DeletionDatabase, blocker_pid: int) -> int:
    return int(
        await database.pool.fetchval(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND $1 = ANY(pg_blocking_pids(pid))",
            blocker_pid,
        )
    )


async def _wait_until_blocked(database: DeletionDatabase, blocker_pid: int, expected: int) -> None:
    for _ in range(3_000):
        if await _blocked_count(database, blocker_pid) >= expected:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"expected {expected} backends blocked by pid {blocker_pid}")


async def _stop(*tasks: "asyncio.Task[object]") -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


_SCENE_LOCK = "SELECT pg_advisory_xact_lock($1::int, hashtext($2))"


async def test_concurrent_idempotent_replay_keeps_loser_intent_and_winner_active(
    run_seeded: DeletionDatabase,
) -> None:
    # Given two deliveries of one idempotency key that uploaded different UUID objects.
    intent_a = await _seed_intent(run_seeded, "956/scenes/a.png")
    intent_b = await _seed_intent(run_seeded, "956/scenes/b.png")

    async with _held(run_seeded, _SCENE_LOCK, _RUN, _SCENE) as blocker_pid:
        first = asyncio.create_task(_accept(_row("956/scenes/a.png", intent_a)))
        second = asyncio.create_task(_accept(_row("956/scenes/b.png", intent_b)))
        try:
            await _wait_until_blocked(run_seeded, blocker_pid, 2)
        except BaseException:
            await _stop(first, second)
            raise
    # When both race for the scene once the lock is released.
    results = await asyncio.wait_for(asyncio.gather(first, second), _ASYNC_WAIT_SECONDS)

    # Then exactly one asset stays active and both callers observe it.
    assets = await _assets(run_seeded)
    assert len(assets) == 1
    assert assets[0]["is_active"] is True
    winner_key = str(assets[0]["storage_key"])
    assert {str(result["storage_key"]) for result in results} == {winner_key}
    # And only the winner's intent was consumed; the loser's object stays discoverable.
    loser_intent = intent_b if winner_key.endswith("a.png") else intent_a
    assert await _intent_ids(run_seeded) == {loser_intent}


async def test_idempotency_key_reused_by_another_scene_is_rejected_without_side_effects(
    run_seeded: DeletionDatabase,
) -> None:
    # Given an accepted asset for one scene under a shared idempotency key.
    intent_x = await _seed_intent(run_seeded, "956/scenes/x.png", scene="scene-x")
    await _accept(_row("956/scenes/x.png", intent_x, idempotency_key="shared", scene="scene-x"))
    intent_y = await _seed_intent(run_seeded, "956/scenes/y.png", scene="scene-y")

    # When a different scene reuses that key.
    with pytest.raises(SceneAssetAcceptanceRejected):
        await _accept(_row("956/scenes/y.png", intent_y, idempotency_key="shared", scene="scene-y"))

    # Then nothing is deactivated, nothing is inserted and the intent is retained.
    assets = await _assets(run_seeded)
    assert [(a["scene_id"], a["is_active"]) for a in assets] == [("scene-x", True)]
    assert await _intent_ids(run_seeded) == {intent_y}


async def test_exact_replay_of_accepted_upload_returns_existing_asset_without_an_intent(
    run_seeded: DeletionDatabase,
) -> None:
    # Given an accepted upload whose intent was consumed with the asset.
    intent = await _seed_intent(run_seeded, "956/scenes/a.png")
    first = await _accept(_row("956/scenes/a.png", intent))
    assert await _intent_ids(run_seeded) == set()

    # When the same delivery replays acceptance after a lost acknowledgement.
    replay = await _accept(_row("956/scenes/a.png", intent))

    # Then it is idempotent: same asset, no second version, no rejection.
    assert replay["id"] == first["id"]
    assert len(await _assets(run_seeded)) == 1


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-id", "compat-no-id"])
async def test_acceptance_rejects_a_fenced_intent(
    run_seeded: DeletionDatabase, explicit: bool,
) -> None:
    # Given reclaim already fenced the upload intent.
    key = "956/scenes/fenced.png"
    intent = await _seed_intent(run_seeded, key, fenced=True)

    # When acceptance is attempted through either the id or the key-only path.
    with pytest.raises(SceneAssetAcceptanceRejected):
        await _accept(_row(key, intent if explicit else None))

    # Then no scene references the possibly deleted object and the fence is untouched.
    assert await _assets(run_seeded) == []
    assert await _intent_ids(run_seeded) == {intent}


async def test_explicit_and_compat_acceptance_for_one_upload_cannot_deadlock(
    run_seeded: DeletionDatabase,
) -> None:
    # Given the id path and the key-only path racing for one upload and scene.
    key = "956/scenes/shared-order.png"
    intent = await _seed_intent(run_seeded, key)

    async with _held(run_seeded, _SCENE_LOCK, _RUN, _SCENE) as blocker_pid:
        compat = asyncio.create_task(_accept(_row(key, None)))
        explicit: "asyncio.Task[dict[str, object]] | None" = None
        try:
            await _wait_until_blocked(run_seeded, blocker_pid, 1)
            explicit = asyncio.create_task(_accept(_row(key, intent)))
            for _ in range(3_000):
                if explicit.done() or await _blocked_count(run_seeded, blocker_pid) >= 2:
                    break
                await asyncio.sleep(0.01)
        except BaseException:
            await _stop(*(task for task in (compat, explicit) if task is not None))
            raise
    assert explicit is not None

    # When the scene lock is released both attempts finish.
    outcomes = await asyncio.wait_for(
        asyncio.gather(compat, explicit, return_exceptions=True), _ASYNC_WAIT_SECONDS,
    )

    # Then lock ordering is consistent: no deadlock victim, one winner, the rest rejected.
    assert not any(isinstance(o, asyncpg.exceptions.DeadlockDetectedError) for o in outcomes)
    assert all(isinstance(o, (dict, SceneAssetAcceptanceRejected)) for o in outcomes)
    assert sum(isinstance(o, dict) for o in outcomes) == 1
    assert len(await _assets(run_seeded)) == 1
    assert await _intent_ids(run_seeded) == set()


async def test_acceptance_is_rejected_promptly_while_cleanup_holds_the_intent_row(
    run_seeded: DeletionDatabase,
) -> None:
    # Given reclaim holds the intent row lock across a slow storage deletion.
    key = "956/scenes/slow-delete.png"
    intent = await _seed_intent(run_seeded, key)

    async with _held(
        run_seeded, "SELECT id FROM creator_artifacts WHERE id = $1 FOR UPDATE", intent,
    ):
        # When acceptance arrives it fails closed instead of queueing behind the deletion.
        with pytest.raises(SceneAssetAcceptanceRejected):
            await asyncio.wait_for(_accept(_row(key, intent)), timeout=10)

        # Then it released its project/run share locks, so a deletion fence is not stalled.
        await asyncio.wait_for(
            run_seeded.pool.execute("UPDATE creator_projects SET status = 'deleting' WHERE id = 1"),
            timeout=10,
        )

    assert await _assets(run_seeded) == []
    assert await _intent_ids(run_seeded) == {intent}


async def _register(storage_key: str) -> int:
    return await rejected_scene_cleanup_service.record_intent(
        run_id=_RUN,
        workspace_id=1,
        project_id=1,
        storage_key=storage_key,
        local_path=f"/tmp/{storage_key.rsplit('/', 1)[-1]}",
        storage_provider="local",
        content_type="image/png",
    )


async def test_registration_records_an_intent_for_an_open_run(run_seeded: DeletionDatabase) -> None:
    intent = await _register("956/scenes/open.png")

    assert await _intent_ids(run_seeded) == {intent}
    assert await _register("956/scenes/open.png") == intent


@pytest.mark.parametrize(
    "fence",
    [
        "UPDATE creator_projects SET status = 'deleting' WHERE id = 1",
        "UPDATE creator_runs SET status = 'cancelled' WHERE id = 956",
    ],
    ids=["project-deleting", "run-cancelled"],
)
async def test_registration_is_refused_after_a_deletion_or_cancel_fence(
    run_seeded: DeletionDatabase, fence: str,
) -> None:
    # Given the deletion/cancel barrier has committed.
    await run_seeded.pool.execute(fence)

    # When an uploader tries to register behind the artifact enumeration.
    with pytest.raises(SceneUploadIntentRejected):
        await _register("956/scenes/late.png")

    # Then no intent row exists, so the upload never starts.
    assert await _intent_ids(run_seeded) == set()


async def test_registration_waits_for_an_uncommitted_deletion_fence_then_refuses(
    run_seeded: DeletionDatabase,
) -> None:
    # Given a deletion fence that has not committed yet.
    async with _held(
        run_seeded, "UPDATE creator_projects SET status = 'deleting' WHERE id = 1", commit=True,
    ) as blocker_pid:
        registration = asyncio.create_task(_register("956/scenes/racing.png"))
        try:
            await _wait_until_blocked(run_seeded, blocker_pid, 1)
        except BaseException:
            await _stop(registration)
            raise
    # When the fence commits, the waiting registration observes it.
    with pytest.raises(SceneUploadIntentRejected):
        await asyncio.wait_for(registration, _ASYNC_WAIT_SECONDS)

    assert await _intent_ids(run_seeded) == set()


class _AmbiguousDeleteBackend(LocalStorageBackend):
    def delete(self, key: str) -> bool:
        super().delete(key)
        raise TimeoutError("delete outcome unknown")


@pytest.mark.parametrize("explicit", [True, False], ids=["explicit-id", "compat-no-id"])
async def test_acceptance_after_an_ambiguous_reclaim_delete_is_rejected(
    run_seeded: DeletionDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    # Given reclaim fenced an aged intent, deleted its bytes, then lost the acknowledgement.
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    backend = _AmbiguousDeleteBackend(str(tmp_path / "objects"))
    key = "956/scenes/ambiguous.png"
    backend.upload(key, b"bytes", "image/png")
    source = tmp_path / "ambiguous.png"
    source.write_bytes(b"source")
    intent = await _seed_intent(run_seeded, key, file_path=str(source))
    await run_seeded.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE id = $1",
        intent,
    )

    @contextlib.asynccontextmanager
    async def transaction() -> AsyncIterator[asyncpg.Connection]:
        async with run_seeded.pool.acquire() as connection, connection.transaction():
            yield connection

    assert await retry_failed_deletions(5, transaction, lambda: backend) == 0
    assert not backend.exists(key)
    fence = await run_seeded.pool.fetchrow(
        "SELECT delete_failed_at, delete_retry_count FROM creator_artifacts WHERE id = $1",
        intent,
    )
    assert fence is not None
    assert fence["delete_failed_at"] is not None
    assert fence["delete_retry_count"] == 1

    # When a slow uploader then tries to accept that same, possibly deleted, object.
    with pytest.raises(SceneAssetAcceptanceRejected):
        await _accept(_row(key, intent if explicit else None))

    # Then no scene references it; the fenced row stays so cleanup can finish.
    assert await _assets(run_seeded) == []
    assert await _intent_ids(run_seeded) == {intent}


async def test_registration_accepts_real_worker_filenames_for_every_task_and_scene_shape(
    run_seeded: DeletionDatabase,
) -> None:
    # Given the names the worker really generates: <scene>-<task>-<host>-<pid>-<uuid>.png,
    # for a celery task id, the lightweight dispatcher id, and scene ids up to the model limit.
    task_ids = {
        "celery": str(uuid4()),
        "lightweight": f"sync-generate_scene_image-{_RUN}-{uuid4().hex[:12]}",
    }
    scene_ids = ("scene-1", "scene-10", "s" * 100)
    registered: dict[str, int] = {}
    failures: list[str] = []

    # When each upload registers its cleanup intent before object I/O.
    for task_label, task_id in task_ids.items():
        owner = delivery_owner(task_id, "worker-host-1", 12345)
        for scene_id in scene_ids:
            safe_scene = sanitize_path_component(scene_id, label="scene_id")
            name = f"{safe_scene}-{owner}-{uuid4().hex}.png"
            key = f"{_RUN}/scenes/{name}"
            try:
                registered[key] = await rejected_scene_cleanup_service.record_intent(
                    run_id=_RUN,
                    workspace_id=1,
                    project_id=1,
                    storage_key=key,
                    local_path=f"/data/artifacts/{key}",
                    storage_provider="local",
                    content_type="image/png",
                )
            except asyncpg.PostgresError as error:
                failures.append(f"{task_label}/{scene_id[:10]}/{len(Path(name).stem)}: {error}")

    # Then none overflows a bounded bookkeeping column, so no valid scene fails before upload.
    assert failures == []
    assert await _intent_ids(run_seeded) == set(registered.values())
