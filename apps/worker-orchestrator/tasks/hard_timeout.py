from __future__ import annotations

import logging
import os
from collections.abc import Callable
from hashlib import blake2s
from typing import Any

from celery import Task
from celery.utils.nodenames import gethostname
from celery.worker.request import Request
from creator_domain.sanitize import sanitize_path_component

logger = logging.getLogger(__name__)


def delivery_owner(task_id: str, worker_hostname: str, worker_pid: int) -> str:
    safe_task_id = sanitize_path_component(task_id, label="task_id")
    worker_digest = blake2s(worker_hostname.encode(), digest_size=8).hexdigest()
    return f"{safe_task_id}-{worker_digest}-{worker_pid}"


def _terminal_services() -> Any:
    from creator_service.task_tracking_service import task_tracking_service
    from creator_service.usage_service import usage_service

    return task_tracking_service.fail_claimed_and_cancel_quota


def finalize_hard_timeout(
    task_id: str,
    claim_token: str,
    run_id: int,
    timeout: float,
    safe_failure_stages: frozenset[str],
) -> None:
    from worker_loop import run_in_worker_loop

    finalize_claimed = _terminal_services()

    async def finalize() -> None:
        from creator_service.run_service import run_service
        from creator_service.usage_service import usage_service

        await finalize_claimed(
            task_id,
            claim_token,
            usage_service.storage,
            "TIMEOUT",
            f"The task exceeded its {timeout} second hard time limit",
            run_service.storage,
            run_id,
            safe_failure_stages,
        )

    run_in_worker_loop(finalize())


class ProductionRequest(Request):
    def on_timeout(self, soft: bool, timeout: float) -> None:
        super().on_timeout(soft, timeout)
        if soft or not isinstance(self.id, str):
            return
        positional_run_id = self.args[0] if self.args else None
        keyword_run_id = self.kwargs.get("run_id")
        if positional_run_id is not None and keyword_run_id not in (None, positional_run_id):
            return
        run_id = positional_run_id if positional_run_id is not None else keyword_run_id
        safe_failure_stages = self.task.hard_timeout_safe_failure_stages
        if (
            type(run_id) is int
            and isinstance(self.worker_pid, int)
            and isinstance(safe_failure_stages, frozenset)
        ):
            hostname = gethostname()
            owner = delivery_owner(self.id, hostname, self.worker_pid)
            try:
                finalize_hard_timeout(
                    self.id,
                    owner,
                    run_id,
                    timeout,
                    safe_failure_stages,
                )
            except Exception:
                logger.error("Hard-timeout finalization failed for run %d", run_id, exc_info=True)
            try:
                self.task.hard_timeout_cleanup(
                    run_id,
                    self.id,
                    hostname,
                    self.worker_pid,
                )
            except Exception:
                logger.warning("Hard-timeout cleanup failed for run %d", run_id, exc_info=True)


class ProductionTask(Task):
    Request = ProductionRequest
    hard_timeout_safe_failure_stages: frozenset[str] = frozenset()
    hard_timeout_cleanup: Callable[[int, str, str, int], None] = staticmethod(
        lambda _run_id, _task_id, _worker_hostname, _worker_pid: None
    )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.request.claim_token = delivery_owner(
            self.request.id,
            gethostname(),
            os.getpid(),
        )
        return super().__call__(*args, **kwargs)


def production_task(
    safe_failure_stages: frozenset[str],
    cleanup: Callable[[int, str, str, int], None] | None = None,
) -> type[ProductionTask]:
    class ConfiguredProductionTask(ProductionTask):
        hard_timeout_safe_failure_stages = safe_failure_stages
        hard_timeout_cleanup = staticmethod(
            cleanup
            or (lambda _run_id, _task_id, _worker_hostname, _worker_pid: None)
        )

    return ConfiguredProductionTask


def production_time_limits(soft: int, hard: int) -> tuple[int | None, int]:
    override = os.getenv("WORKER_TOPOLOGY_PRODUCTION_HARD_TIME_LIMIT")
    if (
        os.getenv("ENVIRONMENT") == "test"
        and os.getenv("WORKER_TOPOLOGY_PROBE_ENABLED") == "1"
        and os.getenv("WORKER_TOPOLOGY_ALLOW_PRODUCTION_TIMEOUT_OVERRIDE") == "1"
        and override is not None
    ):
        try:
            override_hard = int(override)
        except ValueError:
            return soft, hard
        if override_hard > 0:
            return None, override_hard
    return soft, hard
