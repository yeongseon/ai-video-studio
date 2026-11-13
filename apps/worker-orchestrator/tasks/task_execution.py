"""Synchronous delivery boundary and execution-scoped failure policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from celery.exceptions import Ignore, SoftTimeLimitExceeded
from creator_provider.exceptions import ProviderTimeoutError, RateLimitError
from creator_domain.models.stage import RunStage
from creator_service.storage_policy import FilesystemLowSpace

if TYPE_CHECKING:
    from tasks.task_runner import TaskContext, TaskResult, TaskRunnerConfig


class TaskInputError(ValueError):
    """An explicitly rejected task prerequisite or argument; keep the run editable."""


class TaskRequest(Protocol):
    retries: int


class TaskDelivery(Protocol):
    request: TaskRequest
    max_retries: int


def run_task(
    celery_self: TaskDelivery,
    run_id: int,
    config: TaskRunnerConfig,
    execute: Callable[[TaskContext], Awaitable[TaskResult]],
) -> dict[str, object]:
    """Reject invalid deliveries; apply guarded failure only after execution starts."""
    # Lazy access preserves the task_runner dependency-injection seams and re-export.
    from tasks import task_runner as runner

    request = getattr(celery_self, "request", None)
    raw_args = getattr(request, "args", None)
    raw_kwargs = getattr(request, "kwargs", None)
    if raw_args is None:
        raw_args = ()
    if raw_kwargs is None:
        raw_kwargs = {}
    if not isinstance(raw_args, (list, tuple)):
        raise ValueError(
            f"Malformed broker message: args is {type(raw_args).__name__}, expected list/tuple"
        )
    if not isinstance(raw_kwargs, dict):
        raise ValueError(
            f"Malformed broker message: kwargs is {type(raw_kwargs).__name__}, expected dict"
        )
    message = {
        "run_id": run_id,
        "task_name": config.task_name,
        "args": list(raw_args),
        "kwargs": dict(raw_kwargs),
    }
    validated_message = runner.validate_task_message(message)
    validated_run_id = validated_message["run_id"]
    task_id = str(getattr(request, "id", None) or f"run-{validated_run_id}")
    safe_failure_stages = config.safe_failure_stages or config.safe_stages
    billing_outcome = runner.BillingOutcome.NOT_STARTED
    execution_entered = False
    claimed = False
    from creator_service.task_tracking_service import TaskTrackingService

    claim_token = None
    if not task_id.startswith("run-") and isinstance(
        runner._task_tracking_service, TaskTrackingService
    ):
        delivery_claim_token = getattr(request, "claim_token", None)
        claim_token = (
            delivery_claim_token if isinstance(delivery_claim_token, str) else uuid4().hex
        )
        setattr(request, "claim_token", claim_token)

    def mark_claimed() -> None:
        nonlocal claimed
        claimed = True

    def cancel_owned() -> None:
        from creator_service.usage_service import usage_service

        try:
            runner.run_in_worker_loop(usage_service.cancel_owned(task_id))
        except Exception:
            runner.logger.warning("Failed to cancel owned quota reservation", exc_info=True)

    async def settle_owned_in_worker_loop(*, billable_unknown: bool = False) -> None:
        from creator_service.usage_service import usage_service

        settlement = (
            usage_service.settle_owned_unknown(task_id)
            if billable_unknown
            else usage_service.settle_owned(task_id)
        )
        await settlement

    def settle_owned(*, billable_unknown: bool = False) -> None:
        try:
            runner.run_in_worker_loop(
                settle_owned_in_worker_loop(billable_unknown=billable_unknown)
            )
        except Exception:
            runner.logger.warning("Failed to settle owned quota reservation", exc_info=True)

    async def execute_owned(ctx: TaskContext) -> TaskResult:
        nonlocal billing_outcome, execution_entered
        execution_entered = True
        ctx.delivery_attempt = getattr(celery_self.request, "retries", 0)
        try:
            return await execute(ctx)
        finally:
            billing_outcome = ctx.billing_outcome

    async def execute_and_settle() -> dict[str, object]:
        result = await runner._run_task_inner(
            validated_run_id, task_id, config, execute_owned,
            claim_token=claim_token, on_claim=mark_claimed,
        )
        await settle_owned_in_worker_loop()
        return result

    try:
        return runner.run_in_worker_loop(execute_and_settle())
    except runner.StageGuardError:
        try:
            if claim_token is not None:
                runner.run_in_worker_loop(
                    runner._task_tracking_service.mark_rejected_if_claimed(task_id, claim_token)
                )
            else:
                runner.run_in_worker_loop(runner._task_tracking_service.mark_rejected(task_id, "stage_guard"))
        except Exception:
            runner.logger.warning("Failed to record task rejection", exc_info=True)
        cancel_owned()
        if config.raise_on_stage_guard:
            raise
        raise Ignore()
    except SoftTimeLimitExceeded as exc:
        runner.logger.error("Task %s timed out for run %s", config.task_name, validated_run_id)
        if claim_token is not None:
            try:
                from creator_service.usage_service import usage_service

                code, message = runner._safe_failure_record(exc)
                outcome = runner.run_in_worker_loop(
                    runner._task_tracking_service.fail_claimed_and_cancel_quota(
                        task_id,
                        claim_token,
                        usage_service.storage,
                        code,
                        message,
                        runner._run_service.storage,
                        validated_run_id,
                        safe_failure_stages,
                        billing_outcome,
                    )
                )
            except Exception:
                runner.logger.warning("Failed to record task timeout", exc_info=True)
                raise
            if not outcome:
                raise
        elif not claimed:
            raise
        match billing_outcome:
            case runner.BillingOutcome.NOT_STARTED:
                pass
            case runner.BillingOutcome.ACCOUNTED:
                settle_owned()
            case runner.BillingOutcome.UNKNOWN:
                settle_owned(billable_unknown=True)
        raise
    except Ignore:
        raise
    except Exception as exc:
        if (
            isinstance(exc, (ProviderTimeoutError, RateLimitError))
            and getattr(celery_self.request, "retries", 0)
            < getattr(celery_self, "max_retries", 0)
        ):
            # Failed tracking remains reclaimable by Celery's retried delivery.
            try:
                code, message = runner._safe_failure_record(exc)
                if claim_token is not None:
                    runner.run_in_worker_loop(
                        runner._task_tracking_service.mark_failed_if_claimed(task_id, claim_token, code, message)
                    )
                else:
                    runner.run_in_worker_loop(
                        runner._task_tracking_service.mark_failed_if_running(task_id, code, message)
                    )
            except Exception:
                runner.logger.warning("Failed to mark task as failed before retry", exc_info=True)
            raise
        if isinstance(exc, config.no_fail_transition_exceptions):
            outcome: bool | None = True
            try:
                code, message = runner._safe_failure_record(exc)
                if claim_token is not None:
                    outcome = runner.run_in_worker_loop(
                        runner._task_tracking_service.mark_failed_if_claimed(task_id, claim_token, code, message)
                    )
                else:
                    outcome = runner.run_in_worker_loop(
                        runner._task_tracking_service.mark_failed_if_running(task_id, code, message)
                    )
            except Exception:
                runner.logger.warning("Failed to record task failure", exc_info=True)
            if claim_token is not None and outcome is None:
                raise
            match billing_outcome:
                case runner.BillingOutcome.NOT_STARTED:
                    cancel_owned()
                case runner.BillingOutcome.ACCOUNTED:
                    settle_owned()
                case runner.BillingOutcome.UNKNOWN:
                    settle_owned(billable_unknown=True)
            raise
        # A refusal before execution says nothing about this task's spend: an earlier delivery may
        # have authorized and started a provider attempt before Celery retried it. Let the durable
        # attempt history decide (settle if any attempt exists, otherwise cancel) instead of
        # assuming this delivery's empty local state means "nothing was started".
        history_decides = isinstance(exc, FilesystemLowSpace) and not execution_entered
        try:
            from creator_service.usage_service import usage_service

            runner.run_in_worker_loop(
                runner._handle_general_failure(
                    task_id,
                    validated_run_id,
                    config.task_name,
                    safe_failure_stages,
                    exc,
                    usage_service.storage,
                    claim_token,
                    None if history_decides else billing_outcome,
                )
            )
        except Exception:
            runner.logger.exception("Failed error cleanup for run %d", validated_run_id)
        raise
