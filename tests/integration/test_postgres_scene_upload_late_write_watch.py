import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import asyncpg
import pytest
from creator_service import db, postgres_visual_asset_storage
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.media_upload_reconciliation import LATE_UPLOAD_WATCH_RETENTION
from creator_service.object_storage import LocalStorageBackend
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.rejected_scene_cleanup import (
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    SCENE_UPLOAD_LATE_WATCH_MARKER,
)
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

_KEY = "956/scenes/late.png"
WATCH_ELAPSED = LATE_UPLOAD_WATCH_RETENTION + timedelta(minutes=1)


@pytest.fixture(autouse=True)
def acceptance_pool(deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_visual_asset_storage, "get_pool", db.get_pool)


class CountingBackend(LocalStorageBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.deletes = 0

    def delete(self, key: str) -> bool:
        self.deletes += 1
        return super().delete(key)


@pytest.fixture
async def watch_case(
    deletion_database: DeletionDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[CountingBackend, Path, int]:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    source = tmp_path / "late.png"
    source.write_bytes(b"source")
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (956, 1, 1)",
    )
    intent = int(
        await deletion_database.pool.fetchval(
            """
            INSERT INTO creator_artifacts (
                run_id, workspace_id, project_id, artifact_type, scene_id, file_path,
                storage_key, storage_provider, content_type, metadata_json,
                delete_requested_at, created_at
            ) VALUES (
                956, 1, 1, 'visual_asset', 'late', $1, $2, 'local', 'image/png', $3,
                NOW(), NOW() - INTERVAL '6 minutes'
            ) RETURNING id
            """,
            str(source), _KEY, SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
        )
    )
    return CountingBackend(tmp_path / "objects"), source, intent


def _passes(
    database: DeletionDatabase, backend: CountingBackend,
) -> Callable[[], Awaitable[int]]:
    @contextlib.asynccontextmanager
    async def transaction() -> AsyncIterator[asyncpg.Connection]:
        async with database.pool.acquire() as connection, connection.transaction():
            yield connection

    async def run_pass() -> int:
        return await retry_failed_deletions(5, transaction, lambda: backend)

    return run_pass


async def _row(database: DeletionDatabase, intent: int) -> asyncpg.Record | None:
    return await database.pool.fetchrow(
        "SELECT delete_failed_at, delete_error, delete_retry_count "
        "FROM creator_artifacts WHERE id = $1",
        intent,
    )


async def test_reclaimed_unresolved_upload_keeps_a_bounded_late_write_watch(
    deletion_database: DeletionDatabase, watch_case: tuple[CountingBackend, Path, int],
) -> None:
    # Given an aged intent whose slow upload has not produced its object yet.
    backend, source, intent = watch_case
    run_pass = _passes(deletion_database, backend)

    # When reclaim fences and cleans it for the first time.
    assert await run_pass() == 1

    # Then the fenced row stays as a watch for the late write instead of vanishing.
    row = await _row(deletion_database, intent)
    assert row is not None, "reclaim erased the only durable record of a possibly in-flight upload"
    assert row["delete_failed_at"] is not None
    assert row["delete_error"] == SCENE_UPLOAD_LATE_WATCH_MARKER
    assert row["delete_retry_count"] == 0
    assert not source.exists()

    # And the late write lands afterwards: acceptance is refused, the next pass leaves it alone.
    backend.upload(_KEY, b"late", "image/png")
    with pytest.raises(SceneAssetAcceptanceRejected):
        await PostgresVisualAssetStorage().save_asset({
            "run_id": 956,
            "scene_id": "late",
            "asset_path": str(source),
            "storage_provider": "local",
            "storage_key": _KEY,
            "is_active": True,
            "idempotency_key": "late:upload",
            "upload_intent_id": intent,
        })
    assert await run_pass() == 0
    assert backend.exists(_KEY)
    assert backend.deletes == 1

    # When the watch horizon elapses.
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET delete_failed_at = delete_failed_at - $2::interval "
        "WHERE id = $1",
        intent,
        WATCH_ELAPSED,
    )

    # Then the final sweep removes the late bytes and only then the record.
    assert await run_pass() == 1
    assert not backend.exists(_KEY)
    assert await _row(deletion_database, intent) is None


async def test_failed_final_sweep_retains_the_record_and_counts_the_failure(
    deletion_database: DeletionDatabase,
    watch_case: tuple[CountingBackend, Path, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a watched intent whose horizon elapsed but whose final delete cannot be confirmed.
    backend, _source, intent = watch_case
    run_pass = _passes(deletion_database, backend)
    assert await run_pass() == 1
    await deletion_database.pool.execute(
        "UPDATE creator_artifacts SET delete_failed_at = delete_failed_at - $2::interval "
        "WHERE id = $1",
        intent,
        WATCH_ELAPSED,
    )

    def fail(_key: str) -> bool:
        raise TimeoutError("delete outcome unknown")

    monkeypatch.setattr(backend, "delete", fail)

    # When the final sweep cannot confirm deletion.
    assert await run_pass() == 0

    # Then the record is retained and the failure is accounted, never silently dropped.
    row = await _row(deletion_database, intent)
    assert row is not None
    assert row["delete_retry_count"] == 1
    assert row["delete_error"] != SCENE_UPLOAD_LATE_WATCH_MARKER


async def test_key_only_acceptance_after_reclaim_is_rejected_while_the_watch_lasts(
    deletion_database: DeletionDatabase, watch_case: tuple[CountingBackend, Path, int],
) -> None:
    # Given reclaim already fenced, cleaned and finished with an aged intent.
    backend, source, _intent = watch_case
    assert await _passes(deletion_database, backend)() == 1

    # When a delayed acceptance names only the reclaimed key, with a fresh idempotency key.
    with pytest.raises(SceneAssetAcceptanceRejected):
        await PostgresVisualAssetStorage().save_asset({
            "run_id": 956,
            "scene_id": "late",
            "asset_path": str(source),
            "storage_provider": "local",
            "storage_key": _KEY,
            "is_active": True,
            "idempotency_key": "late:key-only",
            "upload_intent_id": None,
        })

    # Then no scene references the deleted bytes.
    assert await deletion_database.pool.fetchval(
        "SELECT count(*) FROM creator_scene_assets WHERE run_id = 956",
    ) == 0


async def test_a_reclaimer_paused_before_its_second_transaction_cannot_erase_a_fresh_watch(
    deletion_database: DeletionDatabase, watch_case: tuple[CountingBackend, Path, int],
) -> None:
    # Given reclaimer A has committed the fence and stops right before its second transaction.
    backend, source, intent = watch_case
    entries = 0
    reached_second_transaction = asyncio.Event()
    resume = asyncio.Event()

    @contextlib.asynccontextmanager
    async def paused_before_second_transaction() -> AsyncIterator[asyncpg.Connection]:
        nonlocal entries
        entries += 1
        if entries == 2:
            reached_second_transaction.set()
            await resume.wait()
        async with deletion_database.pool.acquire() as connection, connection.transaction():
            yield connection

    reclaimer_a = asyncio.create_task(
        retry_failed_deletions(5, paused_before_second_transaction, lambda: backend),
    )
    await asyncio.wait_for(reached_second_transaction.wait(), timeout=30)

    # When reclaimer B runs a complete pass, which turns the fenced row into a fresh watch.
    assert await _passes(deletion_database, backend)() == 1
    watch = await _row(deletion_database, intent)
    assert watch is not None
    assert watch["delete_error"] == SCENE_UPLOAD_LATE_WATCH_MARKER

    # And A then resumes its second transaction against that very row.
    resume.set()
    assert await asyncio.wait_for(reclaimer_a, timeout=30) == 0

    # Then A neither deleted again nor treated the new watch as a finished one:
    # the late-write watch survives and the key still cannot be accepted.
    assert await _row(deletion_database, intent) is not None, "a stale reclaimer erased the watch"
    assert backend.deletes == 1
    with pytest.raises(SceneAssetAcceptanceRejected):
        await PostgresVisualAssetStorage().save_asset({
            "run_id": 956,
            "scene_id": "late",
            "asset_path": str(source),
            "storage_provider": "local",
            "storage_key": _KEY,
            "is_active": True,
            "idempotency_key": "late:after-race",
            "upload_intent_id": None,
        })
