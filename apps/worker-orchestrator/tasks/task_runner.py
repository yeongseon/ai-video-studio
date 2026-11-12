"""Common task runner infrastructure for Celery worker tasks.

Extracts cross-cutting concerns shared by all generation tasks:
- Task tracking (start, running, success, failed, rejected)
- Stage guard validation
- Workspace/project context resolution
- GPU lock acquisition/release
- Provider usage recording
- Conditional stage transitions (success/failure)
- Active task ID cleanup

Usage:
    @celery_app.task(bind=True, ...)
    @trace_task("generate_foo")
    def generate_foo(self, run_id: int, ...) -> dict[str, object]:
        config = TaskRunnerConfig(
            task_name="generate_foo",
            allowed_stages=frozenset({RunStage.FOO_GENERATING}),
            safe_stages=frozenset({RunStage.FOO_GENERATING.value}),
            success_stage=RunStage.FOO_REVIEW.value,
            requires_free_space=False,  # True for tasks that write media (video, image, audio)
        )

        async def execute(ctx: TaskContext) -> TaskResult:
            # Your task-specific logic here
            result = await provider.generate(...)
            await save_result(...)
            return TaskResult(status="success", extra={"my_field": value})

        return run_task(self, run_id, config, execute)
"""

# pyright: reportMissingImports=false
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from creator_service.usage_service import UsageStorageBackend
from creator_service.task_tracking_storage import BillingOutcome

from celery.exceptions import Ignore, SoftTimeLimitExceeded
from creator_domain.models.stage import REVIEW_STAGES, RunStage
from creator_provider.gpu_lock import (
    GPU_LOCK_TIMEOUT_SECONDS,
    acquire_gpu_lock,
    release_gpu_lock,
    renew_gpu_lock,
)
from creator_provider.versioned_assets import clear_loaded_asset_versions, get_loaded_asset_versions
from creator_service.actionable_errors import build_run_failure_summary
from creator_service.job_capacity import ensure_job_capacity
from creator_service.run_service import run_service as _run_service
from creator_service.task_tracking_service import task_tracking_service as _task_tracking_service
from creator_service.usage_service import resolve_workspace_id_from_run
from tasks.task_execution import TaskInputError, run_task as run_task


redis: Any
try:
    import redis
except ImportError:
    redis = None


def run_in_worker_loop(coro):
    """Lazy-import wrapper to avoid import errors in non-worker contexts (e.g. API)."""
    from worker_loop import run_in_worker_loop as _run
    return _run(coro)


logger = logging.getLogger(__name__)

# Statuses that must never be overwritten by worker completion transitions.
_TERMINAL_STATUSES = frozenset({"cancelled"})


@dataclass
class TaskRunnerConfig:
    """Configuration for a task runner invocation."""

    task_name: str
    allowed_stages: frozenset[RunStage]
    safe_stages: frozenset[str]
    success_stage: str | None = None
    safe_failure_stages: frozenset[str] | None = None  # defaults to safe_stages
    skip_stage_guard: bool = False
    raise_on_stage_guard: bool = True
    no_fail_transition_exceptions: tuple[type[Exception], ...] = (TaskInputError,)
    # Media-writing tasks (video, image, audio) are refused before their first write when the
    # artifact or scratch filesystem is below its free-space watermark.
    requires_free_space: bool = False


@dataclass
class TaskContext:
    """Context passed to the task execution function."""

    run_id: int
    task_id: str
    run: dict[str, Any]
    workspace_id: int | None
    project_id: int | None
    start_time: datetime
    reservation_owner_id: str | None = None
    delivery_attempt: int = 0
    billing_outcome: BillingOutcome = field(default=BillingOutcome.NOT_STARTED, init=False)

    def mark_provider_started(self) -> None:
        self.billing_outcome = BillingOutcome.UNKNOWN

    def mark_provider_accounted(self) -> None:
        self.billing_outcome = BillingOutcome.ACCOUNTED


@dataclass
class TaskResult:
    """Result returned by the task execution function."""

    status: str = "success"
    extra: dict[str, object] = field(default_factory=dict)


class StageGuardError(ValueError):
    """Run is not in an acceptable stage. Do NOT mutate run to FAILED."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_redis_client() -> Any | None:
    if redis is None:
        return None
    return redis.Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))


def validate_artifact_path(path: str, artifact_root: str) -> str:
    resolved = os.path.realpath(path)
    root = os.path.realpath(artifact_root)
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise ValueError(f"Path {path!r} is outside artifact root")
    return resolved


async def _run_task_inner(
    run_id: int,
    task_id: str,
    config: TaskRunnerConfig,
    execute: Callable[[TaskContext], Awaitable[TaskResult]],
    claim_token: str | None = None,
    on_claim: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Inner async execution with full lifecycle management."""
    # Reject new work if graceful shutdown is in progress
    from celery_app import is_shutting_down

    if is_shutting_down():
        logger.info("Rejecting task %s: graceful shutdown in progress", task_id)
        raise Ignore()
    # Reset asset version tracking to isolate from import-time loads
    clear_loaded_asset_versions()
    start_time = datetime.now(timezone.utc)

    try:
        existing_task = await _task_tracking_service.storage.get_by_celery_id(task_id)
        if (
            existing_task is not None
            and not task_id.startswith("run-")
            and existing_task.get("status") == "success"
            and existing_task.get("run_id") == run_id
            and existing_task.get("task_type") == config.task_name
        ):
            logger.info("Task %s already succeeded (idempotency guard), skipping", task_id)
            end_time = datetime.now(timezone.utc)
            return {
                "task_id": task_id,
                "run_id": run_id,
                "status": "success",
                "idempotent_skip": True,
                "asset_versions": {},
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_seconds": 0.0,
            }
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.warning("Failed idempotency guard lookup", exc_info=True)

    # 1. Task tracking: exclusive claim (only one worker executes per task)
    try:
        if claim_token is not None:
            start_result = await _task_tracking_service.record_task_start(
                run_id, config.task_name, task_id, claim_token=claim_token,
            )
        else:
            start_result = await _task_tracking_service.record_task_start(run_id, config.task_name, task_id)
        if not task_id.startswith("run-"):
            if start_result is None:
                logger.info("Task %s already claimed by another worker, skipping", task_id)
                end_time = datetime.now(timezone.utc)
                return {
                    "task_id": task_id,
                    "run_id": run_id,
                    "status": "success",
                    "idempotent_skip": True,
                    "asset_versions": {},
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "duration_seconds": 0.0,
                }
            if start_result.status == "success":
                logger.info("Task %s already succeeded (atomic start guard), skipping", task_id)
                end_time = datetime.now(timezone.utc)
                return {
                    "task_id": task_id,
                    "run_id": run_id,
                    "status": "success",
                    "idempotent_skip": True,
                    "asset_versions": {},
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                    "duration_seconds": 0.0,
                }
        if start_result is not None and start_result.status == "running" and on_claim is not None:
            on_claim()
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        if not task_id.startswith("run-"):
            logger.error("Failed to record task start — refusing to execute without claim", exc_info=True)
            raise
        logger.warning("Failed to record task start (synthetic task ID, continuing)", exc_info=True)

    # 2. Stage guard
    run = await _run_service.storage.get_run(run_id)
    if run is None and not config.skip_stage_guard:
        raise StageGuardError(f"Run {run_id} not found")

    if run is not None and not config.skip_stage_guard:
        try:
            current = RunStage(run["current_stage"])
        except ValueError as exc:
            raise StageGuardError(
                f"Run {run_id} has invalid stage {run['current_stage']!r}"
            ) from exc

        if current not in config.allowed_stages:
            raise StageGuardError(
                f"Run {run_id} is in stage {current.value}, "
                f"expected one of {', '.join(s.value for s in config.allowed_stages)}"
            )
        if run.get("status") == "cancelled":
            raise StageGuardError(f"Run {run_id} is cancelled")

    # 3. Resolve workspace/project context
    workspace_id: int | None = None
    project_id: int | None = None
    if run is not None:
        workspace_id = await resolve_workspace_id_from_run(run_id)
        project_id = run.get("project_id")
    logger.info(
        "Task context resolved",
        extra={
            "task_name": config.task_name,
            "run_id": run_id,
            "workspace_id": workspace_id,
            "project_id": project_id,
        },
    )

    # 3b. Refuse before any output exists when a filesystem this task writes to is low on space.
    # Raised after the claim and stage guard so the refusal is recorded on the task, and before
    # quota re-admission and provider work so nothing is billed and nothing is written.
    if config.requires_free_space:
        ensure_job_capacity(config.task_name, run_id=run_id)

    # 4. Execute task-specific logic
    from creator_service.usage_service import usage_service

    reservation_owner_id = await usage_service.reservation_owner_id(task_id)
    if reservation_owner_id is not None and not await usage_service.readmit_owned(
        reservation_owner_id
    ):
        raise StageGuardError(f"Reservation owner {reservation_owner_id} cannot be re-admitted")
    ctx = TaskContext(
        run_id=run_id,
        task_id=task_id,
        run=run or {},
        workspace_id=workspace_id,
        project_id=project_id,
        start_time=start_time,
        reservation_owner_id=reservation_owner_id,
    )

    result = await execute(ctx)

    if claim_token is not None and config.success_stage is not None:
        success = result.status == "success"
        target_stage = config.success_stage if success else RunStage.FAILED.value
        target_status = (
            "paused" if target_stage in {stage.value for stage in REVIEW_STAGES} else "running"
        ) if success else "failed"
        accepted = await _task_tracking_service.finish_claimed_run(
            task_id, claim_token, "success" if success else "failed", _run_service.storage,
            run_id, {"current_stage": target_stage, "status": target_status},
            config.safe_stages if success else config.safe_failure_stages or config.safe_stages,
            _TERMINAL_STATUSES,
            None if success else "task_result",
            None if success else f"status={result.status}",
        )
        if not accepted:
            logger.info("Task %s lost its claim; skipping run transition", task_id)
        end_time = datetime.now(timezone.utc)
        return {
            "task_id": task_id, "run_id": run_id, "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": (end_time - start_time).total_seconds(),
            "status": result.status, **result.extra, "asset_versions": get_loaded_asset_versions(),
        }

    # 5. Stage transition based on result status
    if config.success_stage is not None:
        safe_failure_stages = config.safe_failure_stages or config.safe_stages
        if result.status == "success":
            # Use "paused" for review stages to indicate awaiting human action
            target_status = "paused" if config.success_stage in {s.value for s in REVIEW_STAGES} else "running"
            applied, _ = await _run_service.storage.conditional_update_run(
                run_id,
                {"current_stage": config.success_stage, "status": target_status},
                expected_stages=config.safe_stages,
                rejected_statuses=_TERMINAL_STATUSES,
            )
            if not applied:
                logger.info(
                    "Run %d stage changed during %s -- skipping success transition",
                    run_id,
                    config.task_name,
                )
        else:
            applied, _ = await _run_service.storage.conditional_update_run(
                run_id,
                {"current_stage": RunStage.FAILED.value, "status": "failed"},
                expected_stages=safe_failure_stages,
                rejected_statuses=_TERMINAL_STATUSES,
            )
            if not applied:
                logger.info(
                    "Run %d stage changed during %s -- skipping FAILED transition",
                    run_id,
                    config.task_name,
                )

    # 6. Mark success/failure based on result status
    end_time = datetime.now(timezone.utc)
    try:
        if result.status == "success" and claim_token is not None:
            await _task_tracking_service.mark_success_if_claimed(task_id, claim_token)
        elif result.status == "success":
            await _task_tracking_service.mark_success_if_running(task_id)
        elif claim_token is not None:
            await _task_tracking_service.mark_failed_if_claimed(
                task_id, claim_token, "task_result", f"status={result.status}"
            )
        else:
            await _task_tracking_service.mark_failed_if_running(
                task_id, "task_result", f"status={result.status}"
            )
    except Exception:
        logger.warning("Failed to record task outcome", exc_info=True)

    return {
        "task_id": task_id,
        "run_id": run_id,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_seconds": (end_time - start_time).total_seconds(),
        "status": result.status,
        **result.extra,
        "asset_versions": get_loaded_asset_versions(),
    }


def _safe_failure_record(exc: Exception) -> tuple[str, str]:
    """Redacted, categorized (error_code, error_message) for a failed task.

    Uses the SF-78 actionable-error policy so no raw exception text — which may
    embed provider URLs, tokens, or filesystem paths — is persisted or exposed.
    """
    summary = build_run_failure_summary(exc)
    return str(summary["code"]), str(summary["message"])


async def _handle_general_failure(
    task_id: str,
    run_id: int,
    config_task_name: str,
    safe_failure_stages: frozenset[str],
    exc: Exception,
    usage_storage: UsageStorageBackend,
    claim_token: str | None = None,
    billing_outcome: BillingOutcome | None = BillingOutcome.NOT_STARTED,
) -> bool:
    """Consolidate error-handler async work into a single coroutine.

    ``billing_outcome=None`` lets the durable attempt history decide how the reservation ends.
    """
    try:
        code, message = _safe_failure_record(exc)
        if claim_token is not None:
            return await _task_tracking_service.fail_claimed_and_cancel_quota(
                task_id,
                claim_token,
                usage_storage,
                code,
                message,
                _run_service.storage,
                run_id,
                safe_failure_stages,
                billing_outcome,
            )
        else:
            outcome = await _task_tracking_service.mark_failed_if_running(task_id, code, message)
    except Exception:
        logger.warning("Failed to record task failure", exc_info=True)
        return False
    try:
        applied, _ = await _run_service.storage.conditional_update_run(
            run_id,
            {"current_stage": RunStage.FAILED.value, "status": "failed"},
            expected_stages=safe_failure_stages,
            rejected_statuses=_TERMINAL_STATUSES,
        )
        if not applied:
            logger.info(
                "Run %d stage changed during %s -- skipping FAILED transition",
                run_id,
                config_task_name,
            )
    except Exception:
        logger.exception("Failed to mark run %d as FAILED after task error", run_id)
    return outcome is not None


# --- GPU Lock helpers for use in execute() callbacks ---


@dataclass
class GpuLockContext:
    """Manages GPU lock acquisition/release within a task."""

    task_id: str
    redis_client: Any | None = None
    acquired: bool = False
    lock_lost: bool = False
    _token: str | None = None
    acquired_at: str | None = None
    released_at: str | None = None
    _renewal_task: Any | None = None

    def acquire(self, lock_id: str | None = None) -> None:
        """Acquire GPU lock and start auto-renewal. Raises RuntimeError if Redis unavailable."""
        self.redis_client = _get_redis_client()
        if self.redis_client is None:
            raise RuntimeError("Redis client is unavailable; cannot acquire GPU lock")
        self._token = acquire_gpu_lock(self.redis_client, lock_id or self.task_id)
        self.acquired = True
        self.lock_lost = False
        self.acquired_at = _utc_now_iso()
        self.start_auto_renewal()

    def renew(self, timeout: int = GPU_LOCK_TIMEOUT_SECONDS) -> bool:
        """Renew GPU lock lease. Returns True if renewed, False if lock lost."""
        if not self.acquired or self._token is None or self.redis_client is None:
            return False
        return renew_gpu_lock(self.redis_client, self._token, timeout=timeout)

    def start_auto_renewal(self, timeout: int = GPU_LOCK_TIMEOUT_SECONDS) -> None:
        """Start background task to auto-renew the GPU lease at half the timeout interval."""
        if not self.acquired or self._renewal_task is not None:
            return

        interval = max(timeout // 2, 1)

        async def _renew_loop() -> None:
            while self.acquired:
                await asyncio.sleep(interval)
                if not self.acquired:
                    break
                try:
                    ok = self.renew(timeout=timeout)
                    if not ok:
                        self.lock_lost = True
                        self.acquired = False
                        logger.warning("GPU lease renewal failed for %s (lock lost)", self.task_id)
                        break
                except Exception:
                    self.lock_lost = True
                    self.acquired = False
                    logger.warning("GPU lease renewal error for %s", self.task_id, exc_info=True)
                    break

        self._renewal_task = asyncio.create_task(_renew_loop())

    def stop_auto_renewal(self) -> None:
        """Cancel the auto-renewal background task."""
        if self._renewal_task is not None:
            self._renewal_task.cancel()
            self._renewal_task = None

    def release(self, lock_id: str | None = None) -> None:
        """Release GPU lock if acquired. Raises RuntimeError if lock was lost."""
        self.stop_auto_renewal()
        was_lost = self.lock_lost
        if self.acquired and self._token is not None:
            try:
                released = release_gpu_lock(self.redis_client, self._token)
                if released:
                    self.released_at = _utc_now_iso()
                else:
                    logger.warning(
                        "GPU lock release returned False for %s (lock expired or stolen)",
                        lock_id or self.task_id,
                    )
                    was_lost = True
            except Exception:
                logger.exception("Failed to release GPU lock for %s", lock_id or self.task_id)
            finally:
                self.acquired = False
                self._token = None
        if was_lost:
            raise RuntimeError(f"GPU lock was lost during execution for {lock_id or self.task_id}")


def validate_task_message(message: dict[str, Any]) -> dict[str, Any]:
    if "run_id" not in message:
        raise ValueError("missing required field: run_id")
    run_id = message["run_id"]
    if isinstance(run_id, bool) or not isinstance(run_id, int):
        raise ValueError("run_id must be int")
    if run_id <= 0:
        raise ValueError("run_id must be positive")

    if "task_name" in message and not isinstance(message["task_name"], str):
        raise ValueError("task_name must be str")
    if "args" in message and not isinstance(message["args"], list):
        raise ValueError("args must be list")
    if "kwargs" in message and not isinstance(message["kwargs"], dict):
        raise ValueError("kwargs must be dict")
    validated = {k: message[k] for k in ("run_id", "task_name", "args", "kwargs") if k in message}

    # Semantic validation of kwargs
    kwargs = validated.get("kwargs", {})
    if "model_key" in kwargs:
        validate_model_key(kwargs["model_key"])
    for key, value in kwargs.items():
        validate_string_arg(value, key)

    return validated


_FIELD_LENGTH_LIMITS: dict[str, int] = {
    "model_key": 256,
    "instructions": 32000,
    "prompt_override": 4096,
    "language": 16,
    "style": 256,
    "voice_id": 256,
}
_DEFAULT_MAX_STRING_LENGTH = 4096


def validate_model_key(key: str) -> None:
    """Validate model_key against the provider registry."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("model_key must be a non-empty string")
    try:
        from creator_provider.registry import get_default_registry
        registry = get_default_registry()
        registry.resolve(key)
    except KeyError:
        raise ValueError(f"model_key {key!r} is not in the allowed list") from None

def validate_string_arg(value: Any, name: str) -> None:
    """Validate that a string argument doesn't exceed field-specific max length."""
    if not isinstance(value, str):
        return
    max_len = _FIELD_LENGTH_LIMITS.get(name, _DEFAULT_MAX_STRING_LENGTH)
    if len(value) > max_len:
        raise ValueError(f"{name} exceeds maximum length of {max_len}")
