"""Cooperative scene checkpoints and guarded batch completion."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from celery.exceptions import Ignore, SoftTimeLimitExceeded
from creator_domain.models.stage import RunStage
from creator_service import artifact_storage_integration
from creator_service.artifact_download_service import ArtifactDownloadService
from creator_service.rejected_scene_cleanup import rejected_scene_cleanup_service
from creator_service.object_storage import StorageResult, get_storage_backend
from creator_service.run_service import RunService
from tasks import task_runner
from tasks.task_runner import TaskContext, TaskResult

logger = logging.getLogger(__name__)

SAFE_SUCCESS_STAGES = frozenset({
    RunStage.VISUAL_PLAN_REVIEW.value,
    RunStage.VISUAL_ASSET_GENERATING.value,
    RunStage.VISUAL_ASSET_REVIEW.value,
})
SAFE_FAILURE_STAGES = frozenset({
    RunStage.VISUAL_PLAN_REVIEW.value,
    RunStage.VISUAL_ASSET_GENERATING.value,
})


@dataclass
class SceneBatch:
    """Accumulate accepted results; cancellation never rolls back accepted media."""

    ctx: TaskContext
    runs: RunService
    results: list[dict[str, object]] = field(default_factory=list)
    failures: list[dict[str, object]] = field(default_factory=list)
    cancelled: bool = False
    local_outputs: set[Path] = field(default_factory=set)
    upload_cleanup_intents: dict[str, int] = field(default_factory=dict)

    def cleanup_local_outputs(self) -> None:
        for path in self.local_outputs:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Scene local cleanup failed", extra={"run_id": self.ctx.run_id})

    async def checkpoint(self) -> None:
        task = await task_runner._task_tracking_service.storage.get_by_celery_id(self.ctx.task_id)
        run = await self.runs.storage.get_run(self.ctx.run_id)
        if (
            run is None
            or run.get("status") in task_runner._TERMINAL_STATUSES
            or (task is not None and task.get("status") == "revoked")
        ):
            raise Ignore()

    async def backoff(self, seconds: float) -> None:
        # DB cancellation has no wakeup signal; bounded sleeps avoid busy polling.
        remaining = seconds
        while remaining > 0:
            await self.checkpoint()
            interval = min(remaining, 0.5)
            await anyio.sleep(interval)
            remaining -= interval
        await self.checkpoint()

    async def upload(self, target_path: str) -> StorageResult:
        await self.checkpoint()
        backend = get_storage_backend()
        path = Path(target_path)
        expected_key = f"{self.ctx.run_id}/scenes/{path.name}"
        record_id = await rejected_scene_cleanup_service.record_intent(
            run_id=self.ctx.run_id,
            workspace_id=self.ctx.workspace_id,
            project_id=self.ctx.project_id,
            storage_key=expected_key,
            local_path=str(path),
            storage_provider=backend.provider,
            content_type="image/png",
        )
        self.upload_cleanup_intents[expected_key] = record_id
        uploaded = artifact_storage_integration.store_artifact_file(
            self.ctx.run_id, path, "image/png",
        )
        try:
            await self.checkpoint()
        except Ignore:
            # Only the fresh batch-owned UUID key is eligible, never an adapter alias.
            if path in self.local_outputs and uploaded.key == expected_key:
                try:
                    await rejected_scene_cleanup_service.cleanup(
                        record_id,
                        expected_key,
                        str(path),
                    )
                    self.upload_cleanup_intents.pop(expected_key, None)
                except SoftTimeLimitExceeded:
                    raise
                except Exception:
                    # SDK cleanup failures must not replace the already-known cancellation.
                    logger.warning("Scene upload cleanup failed", extra={"run_id": self.ctx.run_id})
            raise
        # Save may commit even if its acknowledgement raises; transfer before awaiting it.
        self.local_outputs.discard(path)
        return uploaded

    async def discard_rejected_upload(self, target_path: str, uploaded: StorageResult) -> None:
        path = Path(target_path)
        expected_key = f"{self.ctx.run_id}/scenes/{path.name}"
        if uploaded.key == expected_key:
            record_id = self.upload_cleanup_intents.get(expected_key)
            if record_id is None:
                record_id = await rejected_scene_cleanup_service.record_intent(
                    run_id=self.ctx.run_id,
                    workspace_id=self.ctx.workspace_id,
                    project_id=self.ctx.project_id,
                    storage_key=uploaded.key,
                    local_path=str(path),
                    storage_provider=uploaded.storage_provider,
                    content_type=uploaded.content_type,
                )
            try:
                await rejected_scene_cleanup_service.cleanup(record_id, uploaded.key, str(path))
            except SoftTimeLimitExceeded:
                raise
            except Exception:
                await ArtifactDownloadService.record_rejected_cleanup_failure(
                    record_id, uploaded.key,
                )
            else:
                self.upload_cleanup_intents.pop(expected_key, None)
                self.local_outputs.discard(path)

    async def finish(self, total: int, metadata: dict[str, object]) -> TaskResult:
        status = "failed" if len(self.failures) == total else "partial" if self.failures else "success"
        try:
            await self.checkpoint()
        except Ignore:
            self.cancelled = True
        if not self.cancelled:
            failed = len(self.failures) == total
            applied, row = await self.runs.storage.conditional_update_run(
                self.ctx.run_id,
                {
                    "current_stage": RunStage.FAILED if failed else RunStage.VISUAL_ASSET_REVIEW,
                    "status": "failed" if failed else "running",
                },
                expected_stages=SAFE_FAILURE_STAGES if failed else SAFE_SUCCESS_STAGES,
                rejected_statuses=task_runner._TERMINAL_STATUSES,
            )
            if not applied and row is not None and row.get("status") in task_runner._TERMINAL_STATUSES:
                self.cancelled = True
        if self.cancelled:
            self.cleanup_local_outputs()
        return TaskResult(
            status="cancelled" if self.cancelled else status,
            extra={
                **metadata,
                "total_scenes": total,
                "succeeded": len(self.results),
                "failed": len(self.failures),
                "scene_results": self.results + self.failures,
            },
        )
