import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from creator_domain.exceptions import ConflictError, ServiceUnavailableError
from creator_domain.models import RunTask
from creator_service import db
from creator_service.postgres_run_storage import PostgresRunStorage
from creator_service.postgres_task_tracking_storage import PostgresTaskTrackingStorage
from creator_service.run_service import InMemoryRunStorage, RunService
from creator_service.task_dispatch_service import TaskDispatchService
from creator_service.task_tracking_service import InMemoryTaskTrackingStorage, TaskTrackingService

from .test_dispatch_port import RecordingDispatcher


@pytest_asyncio.fixture(params=["memory", "postgres"])
async def tracking(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TaskTrackingService]:
    if request.param == "memory":
        yield TaskTrackingService(InMemoryTaskTrackingStorage())
        return
    url = os.getenv("FIRST_SHORT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL required")
    schema = f"revocation_{uuid4().hex}"
    async with asyncpg.create_pool(url, min_size=1, max_size=2) as admin:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=Path(__file__).resolve().parents[3] / "apps/api",
                env={**os.environ, "DATABASE_URL": url, "PGOPTIONS": f"-c search_path={schema}"},
                check=True, capture_output=True, timeout=60,
            )
            async with asyncpg.create_pool(url, min_size=1, max_size=2,
                                           server_settings={"search_path": schema}) as pool:
                await pool.execute("""
                    INSERT INTO users (id, auth_subject) VALUES (1, 'revocation-test');
                    INSERT INTO workspaces (id, name, slug, owner_id) VALUES (1, 'Test', 'test', 1);
                    INSERT INTO creator_projects (id, workspace_id) VALUES (1, 1);
                    INSERT INTO creator_runs (id, project_id, workspace_id) VALUES (1, 1, 1);
                """)

                async def get_pool() -> asyncpg.Pool:
                    return pool

                monkeypatch.setattr(db, "get_pool", get_pool)
                yield TaskTrackingService(PostgresTaskTrackingStorage())
        finally:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [False, True])
async def test_revocation_after_worker_claim_preserves_only_success(tracking: TaskTrackingService, completed: bool) -> None:
    # Given an actual storage-backed task already claimed by the worker.
    await tracking.record_task_pending(1, "render_video", "claimed")
    started = await tracking.record_task_start(1, "render_video", "claimed")
    assert started is not None and started.status == "running"
    original = started
    if completed:
        original = await tracking.mark_success("claimed")
        assert original is not None and original.finished_at is not None
    # When cancellation arrives after the claim (or after success).
    result = await tracking.mark_revoked("claimed")
    # Then running becomes revoked, while completed success cannot be overwritten.
    tasks = await tracking.list_run_tasks(1)
    assert [task.status for task in tasks] == ["success" if completed else "revoked"]
    assert (result is None) == completed
    assert tasks[0].finished_at is not None
    if completed:
        assert tasks[0].finished_at == original.finished_at
        assert tasks[0] == original
    else:
        assert result is not None and result.finished_at is not None
        assert tasks[0].finished_at == result.finished_at


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_run", [False, True])
async def test_dispatch_compensates_when_worker_claims_before_promotion(
    tracking: TaskTrackingService, monkeypatch: pytest.MonkeyPatch, cancel_run: bool,
) -> None:
    # Given real tracking/run storage and a recording broker port.
    if isinstance(tracking.storage, PostgresTaskTrackingStorage):
        runs = RunService(PostgresRunStorage())
        await runs.storage.update_run(
            1, {"current_stage": "SUBTITLE_GENERATING"}, workspace_id=1
        )
        run = await runs.get_run(1, workspace_id=1)
        assert run is not None
    else:
        runs = RunService(InMemoryRunStorage())
        run = await runs.create_run(
            1, None, "default", current_stage="SUBTITLE_GENERATING", workspace_id=1
        )
    port = RecordingDispatcher(queued=True)
    dispatch = TaskDispatchService(port)
    monkeypatch.setattr("creator_service.task_tracking_service.task_tracking_service", tracking)
    promote = tracking.promote_pending_to_queued

    async def race(task_id: str) -> RunTask | None:
        claimed = await tracking.record_task_start(run.id, "render_video", task_id)
        assert claimed is not None and claimed.status == "running"
        if cancel_run:
            await runs.cancel_run(run.id, workspace_id=1)
            return await promote(task_id)
        raise OSError("injected promotion transport failure after worker claim")

    monkeypatch.setattr(tracking, "promote_pending_to_queued", race)
    # When promotion fails or run cancellation races the already-started task.
    with pytest.raises(ConflictError if cancel_run else ServiceUnavailableError):
        await dispatch.cas_dispatch_with_rollback(
            run_id=run.id, expected_stages=frozenset({"SUBTITLE_GENERATING"}),
            target_stage="RENDER_GENERATING", dispatcher=dispatch.dispatch_render_video,
            dispatcher_args={"run_id": run.id, "render_profile": "shorts_default"},
            run_service=runs, rollback_stage="SUBTITLE_GENERATING", rollback_restart_from=None,
            enqueue_error_detail="enqueue failed", workspace_id=1,
        )
    # Then the actual running record is revoked and the broker cancellation is issued.
    tasks = await tracking.list_run_tasks(run.id)
    assert [task.status for task in tasks] == ["revoked"]
    assert port.cancelled == [tasks[0].celery_task_id]
    restored = await runs.get_run(run.id, workspace_id=1)
    assert restored is not None and restored.current_stage == "SUBTITLE_GENERATING"
    if cancel_run:
        assert restored.status == "cancelled"
