from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import asyncpg
import pytest

from creator_service.dispatch_reconciler import DispatchReconciler
from creator_service.memory_task_tracking_storage import InMemoryTaskTrackingStorage
from creator_service.postgres_run_storage import PostgresRunStorage
from creator_service.postgres_task_tracking_storage import PostgresTaskTrackingStorage
from creator_service.run_service import InMemoryRunStorage, RunService
from creator_service.task_tracking_service import TaskTrackingService
from creator_service.usage_service import InMemoryUsageStorage, UsageService

from .quota_dispatch_support import quota_pool as quota_pool


class ScanBarrierTracking(TaskTrackingService):
    def __init__(self, storage: InMemoryTaskTrackingStorage | PostgresTaskTrackingStorage) -> None:
        super().__init__(storage)
        self.scanned = asyncio.Event()
        self.resume = asyncio.Event()

    async def find_stale_pending_tasks(self, threshold_seconds: int = 120):
        tasks = await super().find_stale_pending_tasks(threshold_seconds)
        self.scanned.set()
        await self.resume.wait()
        return tasks


async def _failed_enqueue(celery_task_id: str, task_type: str, run_id: int) -> bool:
    return False


async def _reconcile(tracking: TaskTrackingService, rollback: Callable[[int, str], Awaitable[None]]):
    return await DispatchReconciler(
        task_tracking_service=tracking,
        enqueue_fn=_failed_enqueue,
        rollback_fn=rollback,
        threshold_seconds=-1,
    ).reconcile()


async def _run_id(pool: asyncpg.Pool | None) -> int:
    if pool is None:
        return 1
    project_id = await pool.fetchval(
        "INSERT INTO creator_projects (workspace_id) VALUES (1) RETURNING id"
    )
    return await pool.fetchval(
        """INSERT INTO creator_runs
           (project_id, workspace_id, current_stage, status, style_preset)
           VALUES ($1, 1, 'AUDIO_GENERATING', 'pending', 'default') RETURNING id""",
        project_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_pool", ["memory", "postgres"], indirect=True)
async def test_retry_stage_and_pending_owner_are_published_atomically(
    quota_pool: asyncpg.Pool | None,
) -> None:
    inserted = asyncio.Event()
    resume = asyncio.Event()

    if quota_pool is None:
        run_storage = InMemoryRunStorage()

        class MemoryBarrierStorage(InMemoryTaskTrackingStorage):
            async def create_task(self, row):
                if row.get("celery_task_id") == "attempt-b":
                    inserted.set()
                    await resume.wait()
                return await super().create_task(row)

        storage = MemoryBarrierStorage()
        runs = RunService(run_storage)
        run = await runs.create_run(
            1, None, "default", current_stage="VISUAL_ASSET_REVIEW"
        )
        run_id = run.id
    else:
        run_storage = PostgresRunStorage()

        class PostgresBarrierStorage(PostgresTaskTrackingStorage):
            async def _insert_pending_task(self, connection, row):
                inserted.set()
                await resume.wait()
                return await super()._insert_pending_task(connection, row)

        storage = PostgresBarrierStorage()
        run_id = await _run_id(quota_pool)
        await quota_pool.execute(
            "UPDATE creator_runs SET current_stage='VISUAL_ASSET_REVIEW' WHERE id=$1",
            run_id,
        )

    tracking = TaskTrackingService(storage)
    await tracking.record_task_pending(run_id, "generate_audio", "attempt-a")
    await tracking.mark_failed_if_pending(
        "attempt-a", "reconciler_pending_cleanup", "cleanup"
    )

    dispatch = asyncio.create_task(
        tracking.transition_run_and_record_pending(
            run_storage,
            run_id,
            {"current_stage": "AUDIO_GENERATING"},
            frozenset({"VISUAL_ASSET_REVIEW"}),
            "generate_audio",
            "attempt-b",
            rejected_statuses=frozenset({"cancelled"}),
        )
    )
    await inserted.wait()
    cleanup = asyncio.create_task(
        tracking.rollback_run_if_task_owner(
            "attempt-a",
            run_storage,
            run_id,
            frozenset({"generate_audio"}),
            frozenset({"AUDIO_GENERATING"}),
            frozenset({"cancelled", "completed"}),
        )
    )
    if quota_pool is not None:
        await cleanup

    resume.set()
    transition_ok, _ = await dispatch
    rollback_ok, current = await cleanup
    task_b = await tracking.promote_pending_to_queued("attempt-b")

    assert transition_ok
    assert not rollback_ok
    assert current is not None and current["current_stage"] == "AUDIO_GENERATING"
    assert current["status"] == "pending"
    assert task_b is not None and task_b.status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_pool", ["memory", "postgres"], indirect=True)
@pytest.mark.parametrize("promoted_status", ["queued", "running", "success", "revoked"])
async def test_stale_snapshot_cannot_mutate_promoted_task(
    monkeypatch: pytest.MonkeyPatch,
    quota_pool: asyncpg.Pool | None,
    promoted_status: str,
) -> None:
    storage = PostgresTaskTrackingStorage() if quota_pool is not None else InMemoryTaskTrackingStorage()
    tracking = ScanBarrierTracking(storage)
    usage = UsageService(InMemoryUsageStorage())
    await usage.set_quota(1, monthly_tts_requests=2)
    assert await usage.reserve_owned(1, "tts", "attempt-a")
    assert await usage.reserve_owned(1, "tts", "attempt-b")
    monkeypatch.setattr("creator_service.usage_service.usage_service", usage)
    run_id = await _run_id(quota_pool)
    await tracking.record_task_pending(run_id, "generate_audio", "attempt-a")

    run = {"stage": "AUDIO_GENERATING", "files": ["a.wav", "b.wav"]}
    rollbacks: list[str] = []

    async def rollback(run_id: int, task_id: str) -> None:
        rollbacks.append(task_id)
        run["stage"] = "FAILED"
        run["files"].clear()

    reconcile_task = asyncio.create_task(_reconcile(tracking, rollback))
    await tracking.scanned.wait()
    match promoted_status:
        case "queued":
            await tracking.promote_pending_to_queued("attempt-a")
        case "running":
            await tracking.record_task_start(run_id, "generate_audio", "attempt-a")
        case "success":
            await tracking.mark_success("attempt-a")
        case "revoked":
            await tracking.mark_revoked("attempt-a")
        case unreachable:
            raise AssertionError(unreachable)
    await tracking.record_task_pending(run_id, "generate_audio", "attempt-b")
    await tracking.promote_pending_to_queued("attempt-b")
    tracking.resume.set()

    result = await reconcile_task

    task_a = await storage.get_by_celery_id("attempt-a")
    task_b = await storage.get_by_celery_id("attempt-b")
    assert task_a is not None and task_a["status"] == promoted_status
    assert task_b is not None and task_b["status"] == "queued"
    assert rollbacks == []
    assert run == {"stage": "AUDIO_GENERATING", "files": ["a.wav", "b.wav"]}
    assert result.rolled_back == 0
    assert not await usage.reserve_owned(1, "tts", "replacement")


@pytest.mark.asyncio
async def test_abandoned_pending_fails_and_releases_only_its_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = InMemoryTaskTrackingStorage()
    tracking = TaskTrackingService(storage)
    usage = UsageService(InMemoryUsageStorage())
    await usage.set_quota(1, monthly_tts_requests=2)
    assert await usage.reserve_owned(1, "tts", "attempt-a")
    assert await usage.reserve_owned(1, "tts", "attempt-b")
    monkeypatch.setattr("creator_service.usage_service.usage_service", usage)
    await tracking.record_task_pending(1, "generate_audio", "attempt-a")
    rollbacks: list[str] = []

    async def rollback(run_id: int, task_id: str) -> None:
        rollbacks.append(task_id)

    result = await _reconcile(tracking, rollback)

    task = await storage.get_by_celery_id("attempt-a")
    assert task is not None and task["status"] == "failed"
    assert task["error_code"] == "reconciler_rollback"
    assert rollbacks == ["attempt-a"]
    assert result.rolled_back == 1
    assert await usage.reserve_owned(1, "tts", "replacement")
    assert not await usage.reserve_owned(1, "tts", "overflow")


@pytest.mark.asyncio
async def test_terminal_cleanup_is_retried_after_rollback_error() -> None:
    storage = InMemoryTaskTrackingStorage()
    tracking = TaskTrackingService(storage)
    await tracking.record_task_pending(1, "generate_audio", "attempt-a")
    attempts = 0

    async def rollback(run_id: int, task_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("injected rollback failure")

    first = await _reconcile(tracking, rollback)
    second = await _reconcile(tracking, rollback)

    task = await storage.get_by_celery_id("attempt-a")
    assert first.errors
    assert second.errors == []
    assert attempts == 2
    assert task is not None and task["error_code"] == "reconciler_rollback"


@pytest.mark.asyncio
async def test_terminal_cleanup_is_retried_after_final_mark_error() -> None:
    storage = InMemoryTaskTrackingStorage()

    class FailingFinalMarkTracking(TaskTrackingService):
        failures = 1

        async def finish_reconciliation(
            self, celery_task_id: str, claim_token: str,
            error_code: str, error_message: str,
        ):
            if self.failures:
                self.failures -= 1
                raise OSError("injected final mark failure")
            return await super().finish_reconciliation(
                celery_task_id, claim_token, error_code, error_message
            )

    tracking = FailingFinalMarkTracking(storage)
    await tracking.record_task_pending(1, "generate_audio", "attempt-a")
    rollbacks: list[str] = []

    async def rollback(run_id: int, task_id: str) -> None:
        rollbacks.append(task_id)

    first = await _reconcile(tracking, rollback)
    retryable = await storage.get_by_celery_id("attempt-a")
    second = await _reconcile(tracking, rollback)

    task = await storage.get_by_celery_id("attempt-a")
    assert first.rolled_back == 0
    assert first.errors
    assert retryable is not None and retryable["error_code"] == "reconciler_pending_cleanup"
    assert second.rolled_back == 1
    assert rollbacks == ["attempt-a", "attempt-a"]
    assert task is not None and task["error_code"] == "reconciler_rollback"


@pytest.mark.asyncio
async def test_two_reconcilers_compensate_abandoned_pending_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = InMemoryTaskTrackingStorage()
    first_tracking = ScanBarrierTracking(storage)
    second_tracking = ScanBarrierTracking(storage)
    usage = UsageService(InMemoryUsageStorage())
    await usage.set_quota(1, monthly_tts_requests=1)
    assert await usage.reserve_owned(1, "tts", "attempt-a")
    monkeypatch.setattr("creator_service.usage_service.usage_service", usage)
    await first_tracking.record_task_pending(1, "generate_audio", "attempt-a")
    rollbacks: list[str] = []

    async def rollback(run_id: int, task_id: str) -> None:
        rollbacks.append(task_id)

    first = asyncio.create_task(_reconcile(first_tracking, rollback))
    second = asyncio.create_task(_reconcile(second_tracking, rollback))
    await asyncio.gather(first_tracking.scanned.wait(), second_tracking.scanned.wait())
    first_tracking.resume.set()
    second_tracking.resume.set()
    results = await asyncio.gather(first, second)

    assert rollbacks == ["attempt-a"]
    assert sum(result.rolled_back for result in results) == 1
    assert await usage.reserve_owned(1, "tts", "replacement")
