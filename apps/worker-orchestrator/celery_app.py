# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportMissingImports=false
"""Minimal Celery app configuration with structured JSON logging and DLQ hardening.

Key operational features:
- task_acks_late: Acknowledges messages AFTER execution, enabling redelivery on worker crash
- task_reject_on_worker_lost: Rejects tasks if worker connection is lost (prevents message loss)
- worker_cancel_long_running_tasks_on_connection_loss: Cancels hung tasks on disconnect
- DLQ monitoring: Terminal task failures are stored in Redis list `dlq:creator`

See DLQ_MONITORING.md for operational procedures, alerting setup, and task recovery.
"""

import logging
import os
import json
import signal
import time
from datetime import datetime, timezone
import resource
from pathlib import Path
from typing import Any

from celery import Celery
from celery.signals import after_setup_logger, after_task_publish, beat_init, task_failure, worker_process_init
from importlib import import_module
# Only import worker_loop (signal handlers) when running as a worker process,
# not when imported by the API for task dispatch.
try:
    import worker_loop as _worker_loop_module  # noqa: F401 — register signal handlers
except ImportError:
    _worker_loop_module = None  # API context — worker_loop not available
from creator_service.actionable_errors import build_run_failure_summary
from creator_service.error_redaction import JsonValue, sanitize_error_json as _sanitize_for_dlq
from creator_service.logging_config import setup_json_logging
from kombu import Exchange, Queue
from beat_health import HEARTBEAT_INTERVAL_SECONDS, heartbeat_path, record_heartbeat

redis: Any
try:
    import redis
except ImportError:
    redis = None
from creator_service.production_checks import validate_production_config
try:
    import worker_release_identity as _worker_release_identity_module  # noqa: F401 — register signal handler
except ImportError:
    _worker_release_identity_module = None
from creator_service.celery_dlq_fallback import append_fallback_entry, read_fallback_entries
from creator_service.broker_recovery import BrokerSnapshot


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


MAX_MEMORY_MB = _parse_int_env("MAX_MEMORY_MB", 4096)
_SHUTDOWN_REQUESTED = False
_previous_sigterm_handler: object | None = None
_previous_sigint_handler: object | None = None


def _handle_sigterm(_signum: int, _frame: object | None) -> None:
    global _SHUTDOWN_REQUESTED
    if _SHUTDOWN_REQUESTED:
        return
    _SHUTDOWN_REQUESTED = True
    logging.getLogger(__name__).info("SIGTERM received; beginning Celery graceful shutdown")
    # Chain to previous handler
    prev = _previous_sigterm_handler
    if callable(prev):
        prev(_signum, _frame)


def _handle_sigint(_signum: int, _frame: object | None) -> None:
    global _SHUTDOWN_REQUESTED
    if _SHUTDOWN_REQUESTED:
        return
    _SHUTDOWN_REQUESTED = True
    logging.getLogger(__name__).info("SIGINT received; beginning Celery graceful shutdown")
    # Chain to previous handler
    prev = _previous_sigint_handler
    if callable(prev):
        prev(_signum, _frame)


def is_shutting_down() -> bool:
    """Check if a graceful shutdown has been requested."""
    return _SHUTDOWN_REQUESTED

def _apply_resource_limits() -> None:
    """Memory limits are now enforced via cgroup (Docker deploy.resources.limits.memory).

    RLIMIT_AS was previously used but caused MemoryError on CUDA/torch/ffmpeg
    which reserve large virtual address ranges even when RSS is low (#611).
    The function is kept as a no-op for backward compatibility — callers still
    invoke it during startup.
    """
    logging.getLogger(__name__).info("Memory limit: MAX_MEMORY_MB=%d (enforced via cgroup, not RLIMIT_AS)", MAX_MEMORY_MB)


redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
dlq_max_size = max(1, int(os.getenv("DLQ_MAX_SIZE", "10000")))
dlq_fallback_path = os.getenv("DLQ_FALLBACK_PATH", "/tmp/dlq_fallback.jsonl")
BEAT_SCHEDULE: dict[str, dict[str, str | float | dict[str, float]]] = {
    "beat-heartbeat": {
        "task": "beat_heartbeat", "schedule": HEARTBEAT_INTERVAL_SECONDS,
        "options": {"expires": HEARTBEAT_INTERVAL_SECONDS},
    },
    "reconcile-stale-dispatches": {"task": "reconcile_stale_dispatches", "schedule": 60.0},
    "retry-failed-artifact-deletions": {
        "task": "retry_failed_artifact_deletions", "schedule": 300.0,
    },
    "sweep-expired-artifacts": {"task": "sweep_expired_artifacts", "schedule": 600.0},
    "reconcile-media-uploads": {"task": "reconcile_media_uploads", "schedule": 600.0},
    "sweep-media-retention": {"task": "sweep_media_retention", "schedule": 3600.0},
}
dlq_fallback_max_bytes = _parse_int_env("DLQ_FALLBACK_MAX_BYTES", 10 * 1024 * 1024)
broker_recovery_epoch_key = os.getenv("BROKER_RECOVERY_EPOCH_KEY", "queue:recovery_epoch")
broker_epoch_read_attempts = _parse_int_env("BROKER_EPOCH_READ_ATTEMPTS", 3)

def _worker_task_modules() -> list[str]:
    modules = [
        "tasks.beat_heartbeat",
        "tasks.generate_script",
        "tasks.generate_visual_plan",
        "tasks.generate_scene_image",
        "tasks.generate_audio",
        "tasks.generate_subtitles",
        "tasks.render_video",
        "tasks.generate_paragraph_audio",
        "tasks.generate_paragraph_subtitles",
        "tasks.reconcile_stale_dispatches",
        "tasks.retry_failed_artifact_deletions",
        "tasks.sweep_expired_artifacts",
        "tasks.reconcile_media_uploads",
        "tasks.sweep_media_retention",
        "tasks.revoked_quota",
    ]
    if (
        os.getenv("WORKER_TOPOLOGY_PROBE_ENABLED") == "1"
        and os.getenv("ENVIRONMENT") == "test"
    ):
        modules.append("tasks.runtime_topology_probe")
    return modules


celery_app = Celery(
    "worker-orchestrator",
    broker=redis_url,
    backend=redis_url,
    include=_worker_task_modules(),
)
celery_app.conf.task_default_queue = "creator"

# Queue definitions: separate queues per pipeline stage for independent scaling.
# Run workers with: celery -A celery_app worker -Q creator,script,image,audio,render
# Or scale specific stages: celery -A celery_app worker -Q image --concurrency=2
celery_app.conf.task_queues = (
    Queue("creator", Exchange("creator", type="direct"), routing_key="creator"),
    Queue("script", Exchange("creator", type="direct"), routing_key="script"),
    Queue("image", Exchange("creator", type="direct"), routing_key="image"),
    Queue("audio", Exchange("creator", type="direct"), routing_key="audio"),
    Queue("render", Exchange("creator", type="direct"), routing_key="render"),
)

# Route tasks to stage-specific queues.
# Override with CELERY_TASK_ROUTES env (JSON) for custom routing.
_default_task_routes = {
    "tasks.generate_script.*": {"queue": "script"},
    "tasks.generate_visual_plan.*": {"queue": "script"},
    "tasks.generate_scene_image.*": {"queue": "image"},
    "tasks.generate_audio.*": {"queue": "audio"},
    "tasks.generate_paragraph_audio.*": {"queue": "audio"},
    "tasks.generate_subtitles.*": {"queue": "audio"},
    "tasks.generate_paragraph_subtitles.*": {"queue": "audio"},
    "tasks.render_video.*": {"queue": "render"},
}
_custom_routes_raw = os.getenv("CELERY_TASK_ROUTES")
if _custom_routes_raw:
    try:
        _custom_routes = json.loads(_custom_routes_raw)
        _default_task_routes.update(_custom_routes)
    except (json.JSONDecodeError, TypeError):
        logging.getLogger(__name__).warning(
            "Invalid CELERY_TASK_ROUTES JSON; using defaults"
        )
celery_app.conf.task_routes = _default_task_routes


# DLQ Configuration: Controls message acknowledgment and failure handling
# - task_acks_late=True ensures at-least-once delivery by acknowledging AFTER execution
# - task_reject_on_worker_lost=True prevents message loss if worker dies
# This ensures failed tasks are not lost and can be replayed from the DLQ.
celery_app.conf.beat_schedule = BEAT_SCHEDULE
celery_app.conf.update(
    # Prefetch only 1 task per worker to prevent queue saturation
    worker_prefetch_multiplier=1,
    # Acknowledge messages AFTER successful execution (enables redelivery on crash)
    task_acks_late=True,
    # Reject tasks if worker connection is lost (returns to queue, not to DLQ)
    task_reject_on_worker_lost=True,
    # Cancel long-running tasks if worker disconnects
    worker_cancel_long_running_tasks_on_connection_loss=True,
)

validate_production_config(service_kind="worker")
def _register_signal_handlers() -> None:
    """Register shutdown signal handlers. Only safe in main thread of worker process."""
    global _previous_sigterm_handler, _previous_sigint_handler
    try:
        _previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_sigterm)
        _previous_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_sigint)
    except ValueError:
        logging.getLogger(__name__).debug("Skipping signal handler registration: not in main thread")


def _record_heartbeat_dispatch(sender: str | None = None, **_kwargs: object) -> None:
    """Runs only in the Beat process, after the broker accepted a published message."""
    if sender == "beat_heartbeat":
        record_heartbeat(heartbeat_path(), datetime.now(timezone.utc))


@beat_init.connect(weak=False)
def start_recording_heartbeat_dispatches(**_kwargs: object) -> None:
    """Only the Beat process vouches for its own liveness; API and worker processes import this
    module too but never start Beat, so they must not write the receipt."""
    after_task_publish.connect(_record_heartbeat_dispatch, weak=False, dispatch_uid="beat-heartbeat-receipt")


@after_setup_logger.connect
def setup_celery_logger(logger: logging.Logger, **_kwargs: object) -> None:
    """Configure Celery logger with JSON formatting."""
    setup_json_logging(service_name="worker", level="INFO")


@worker_process_init.connect
def setup_worker_process_telemetry(**kwargs: object) -> None:
    """
    Configure OpenTelemetry once per worker process.

    Called automatically when a Celery worker process is initialized.
    Safe across forked workers: each worker process calls init_telemetry
    independently. The idempotency guard in init_telemetry() ensures that
    multiple calls within the same process are no-ops.
    """
    _ = kwargs
    _register_signal_handlers()
    _apply_resource_limits()
    telemetry_module = import_module("creator_service.telemetry")
    telemetry_module.init_telemetry(service_name="worker")



def _record_failed_task_to_dlq(
    task_id: str | None,
    task_name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
    exception: BaseException,
) -> None:
    meter = import_module("creator_service.telemetry").get_meter(__name__)
    retention_failures = meter.create_counter("worker.dlq.retention_failures")
    retained_entries = meter.create_counter("worker.dlq.retained_entries")
    failure = build_run_failure_summary(exception)
    payload = _sanitize_for_dlq({
        "task_id": task_id,
        "task_name": task_name,
        "exception": failure["message"],
        "failure": failure,
        "args": args,
        "kwargs": kwargs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    def _write_dlq_fallback(entry: JsonValue, error: BaseException | None = None) -> None:
        logger = logging.getLogger(__name__)
        try:
            append_fallback_entry(
                Path(dlq_fallback_path), entry, max_bytes=dlq_fallback_max_bytes
            )
            retained_entries.add(1, {"source": "fallback"})
            logger.warning(
                "DLQ Redis write failed; wrote entry to fallback file",
                extra={"dlq_retention_failure": 0, "dlq_source": "fallback"},
            )
        except (OSError, ValueError):
            retention_failures.add(1, {"source": "fallback"})
            logger.error(
                "DLQ Redis write failed and fallback file write also failed",
                extra={"dlq_retention_failure": 1, "dlq_source": "fallback"},
            )
        if error is not None:
            logger.error(
                "DLQ Redis write failure",
            )

    if redis is None:
        _write_dlq_fallback(payload)
        return

    try:
        client = redis.Redis.from_url(redis_url)
        client.lpush("dlq:creator", json.dumps(payload, allow_nan=False))
        client.ltrim("dlq:creator", 0, dlq_max_size - 1)
        retained_entries.add(1, {"source": "redis"})
    except Exception as redis_error:
        _write_dlq_fallback(payload, redis_error)


def read_dlq_fallback(limit: int = 100) -> list[dict[str, JsonValue]]:
    return read_fallback_entries(Path(dlq_fallback_path), limit=limit)


def ensure_broker_epoch() -> BrokerSnapshot:
    if redis is None:
        raise RuntimeError("Redis client unavailable")
    client = redis.Redis.from_url(redis_url)
    stored = None
    for attempt in range(broker_epoch_read_attempts):
        try:
            started_at = datetime.now(timezone.utc)
            generated = f"{os.urandom(16).hex()}|{started_at.isoformat()}"
            client.set(broker_recovery_epoch_key, generated, nx=True)
            stored = client.get(broker_recovery_epoch_key)
            break
        except redis.exceptions.RedisError:
            if attempt + 1 == broker_epoch_read_attempts:
                raise
            time.sleep(0.05 * (attempt + 1))
    if not isinstance(stored, (bytes, str)):
        raise RuntimeError("Broker recovery epoch unavailable")
    decoded = stored.decode() if isinstance(stored, bytes) else stored
    epoch, encoded_started_at = decoded.split("|", maxsplit=1)
    return BrokerSnapshot(
        epoch=epoch,
        started_at=datetime.fromisoformat(encoded_started_at),
        queue_length=0,
        oldest_wait_seconds=None,
        workers_online=0,
    )


def _should_record_to_dlq(sender: Any, exception: BaseException | None = None) -> bool:
    from creator_provider.exceptions import ProviderTimeoutError, RateLimitError

    if not isinstance(exception, (ProviderTimeoutError, RateLimitError)):
        return True

    request = getattr(sender, "request", None)
    retries = getattr(request, "retries", None)
    max_retries = getattr(sender, "max_retries", None)
    if isinstance(retries, int) and isinstance(max_retries, int):
        return retries >= max_retries
    return True


def _terminal_failure_services() -> Any:
    from creator_service.task_tracking_service import task_tracking_service

    return task_tracking_service.fail_claimed_and_cancel_quota


@task_failure.connect
def handle_task_failure(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: tuple[Any, ...] = (),
    kwargs: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    if exception is None:
        return
    if not _should_record_to_dlq(sender, exception):
        return

    if isinstance(task_id, str):
        from worker_loop import run_in_worker_loop

        failure = build_run_failure_summary(exception)
        request = getattr(sender, "request", None)
        claim_token = getattr(request, "claim_token", None)
        try:
            if isinstance(claim_token, str):
                from creator_service.usage_service import usage_service

                finalize_claimed = _terminal_failure_services()
                run_in_worker_loop(
                    finalize_claimed(
                        task_id,
                        claim_token,
                        usage_service.storage,
                        str(failure["code"]),
                        str(failure["message"]),
                    )
                )
        except Exception:
            logging.getLogger(__name__).warning(
                "Failed to finalize terminal task failure",
                extra={"task_id": task_id},
                exc_info=True,
            )

    task_name = getattr(sender, "name", "unknown_task")
    _record_failed_task_to_dlq(
        task_id=task_id,
        task_name=task_name,
        args=args,
        kwargs=kwargs or {},
        exception=exception,
    )
