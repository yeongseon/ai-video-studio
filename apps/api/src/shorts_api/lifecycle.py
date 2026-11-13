import asyncio
import contextlib
import logging
import os
import resource
import signal
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from creator_service.db import close_pool
from creator_service.production_checks import ProductionConfigError
from fastapi import FastAPI

logger = logging.getLogger(__name__)


@dataclass
class ShutdownState:
    is_shutting_down: bool = False
    inflight_requests: int = 0


shutdown_state = ShutdownState()
_previous_sigterm_handler: object | None = None
_previous_sigint_handler: object | None = None


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %d", name, raw, default)
        return default
    if parsed <= 0:
        logger.warning("Non-positive %s=%r; using default %d", name, raw, default)
        return default
    return parsed


MAX_MEMORY_MB = _parse_int_env("MAX_MEMORY_MB", 1024)
MAX_CPU_PERCENT = _parse_int_env("MAX_CPU_PERCENT", 80)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ENABLE_PROCESS_RESOURCE_GUARD = os.getenv("ENABLE_PROCESS_RESOURCE_GUARD", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _apply_resource_limits() -> None:
    """Memory limits are now enforced via cgroup (Docker deploy.resources.limits.memory).

    RLIMIT_AS was previously used but caused MemoryError on processes that
    reserve large virtual address ranges (CUDA/torch/ffmpeg) even when RSS
    is low (#611). Kept as a no-op for backward compatibility.
    """
    logger.info(
        "Memory limit: MAX_MEMORY_MB=%d (enforced via cgroup, not RLIMIT_AS)",
        MAX_MEMORY_MB,
    )


def _cpu_usage_percent(
    previous_cpu_seconds: float, previous_wall_seconds: float
) -> tuple[float, float, float]:
    current_cpu_seconds = (
        resource.getrusage(resource.RUSAGE_SELF).ru_utime
        + resource.getrusage(resource.RUSAGE_SELF).ru_stime
    )
    current_wall_seconds = time.monotonic()
    cpu_delta = max(0.0, current_cpu_seconds - previous_cpu_seconds)
    wall_delta = max(1e-6, current_wall_seconds - previous_wall_seconds)
    cpu_percent = (cpu_delta / wall_delta) * 100.0
    return cpu_percent, current_cpu_seconds, current_wall_seconds


async def _monitor_cpu_limit() -> None:
    cpu_seconds = (
        resource.getrusage(resource.RUSAGE_SELF).ru_utime
        + resource.getrusage(resource.RUSAGE_SELF).ru_stime
    )
    wall_seconds = time.monotonic()
    while True:
        await asyncio.sleep(2.0)
        cpu_percent, cpu_seconds, wall_seconds = _cpu_usage_percent(cpu_seconds, wall_seconds)
        if cpu_percent > float(MAX_CPU_PERCENT):
            logger.error(
                "CPU usage %.1f%% exceeded MAX_CPU_PERCENT=%d; enabling shutdown guard",
                cpu_percent,
                MAX_CPU_PERCENT,
            )
            _mark_shutdown()
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except OSError:
                logger.warning("SIGTERM delivery failed; falling back to sys.exit(1)")
                sys.exit(1)


def _mark_shutdown() -> None:
    if shutdown_state.is_shutting_down:
        return
    shutdown_state.is_shutting_down = True
    logger.info("Graceful shutdown initiated; draining in-flight requests")


def _handle_sigterm(_signum: int, _frame: object | None) -> None:
    logger.info("SIGTERM received; enabling graceful shutdown mode")
    _mark_shutdown()
    # Chain to previous handler (e.g. Uvicorn's) so the server can exit gracefully
    prev = _previous_sigterm_handler
    if callable(prev):
        prev(_signum, _frame)


def _handle_sigint(_signum: int, _frame: object | None) -> None:
    logger.info("SIGINT received; enabling graceful shutdown mode")
    _mark_shutdown()
    # Chain to previous handler so the server can exit gracefully
    prev = _previous_sigint_handler
    if callable(prev):
        prev(_signum, _frame)



KNOWN_EXAMPLE_ADMIN_KEYS: Final = frozenset({
    "local-admin-key-change-me",
    "<a-strong-random-string>",
})


def configured_admin_api_key() -> str:
    """Return no credential for publicly shipped examples, even without startup validation."""
    key = os.environ.get("ADMIN_API_KEY", "")
    return "" if key in KNOWN_EXAMPLE_ADMIN_KEYS else key


def validate_admin_api_key(
    environment: str | None = None,
    admin_key: str | None = None,
    *,
    _is_test_runtime: bool | None = None,
) -> None:
    """Reject public examples everywhere and require a 16-character key in production."""
    import os as _os

    # Skip validation only when explicitly requested via parameter
    if _is_test_runtime:
        return

    key = admin_key if admin_key is not None else _os.environ.get("ADMIN_API_KEY", "")
    if key in KNOWN_EXAMPLE_ADMIN_KEYS:
        raise ProductionConfigError(
            "ADMIN_API_KEY is a publicly known example value. "
            "Please unset it (admin API disabled) or generate a new key with openssl rand -hex 32."
        )

    raw_env = environment if environment is not None else _os.getenv("ENVIRONMENT", "development")
    env = raw_env.strip().lower() if raw_env else "development"
    if env != "production":
        return

    if not key or len(key) < 16:
        logger.critical(
            "ADMIN_API_KEY is not set or too short (min 16 chars); "
            "refusing to start in production"
        )
        raise SystemExit(1)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _previous_sigterm_handler, _previous_sigint_handler
    validate_admin_api_key()
    shutdown_state.is_shutting_down = False
    shutdown_state.inflight_requests = 0
    try:
        _previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_sigterm)
        _previous_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, _handle_sigint)
    except ValueError:
        _previous_sigterm_handler = None
        _previous_sigint_handler = None
        logger.debug("Skipping signal handler registration outside main thread")
    if ENABLE_PROCESS_RESOURCE_GUARD:
        _apply_resource_limits()
        logger.info(
            "Resource limits configured: MAX_MEMORY_MB=%d, MAX_CPU_PERCENT=%d",
            MAX_MEMORY_MB,
            MAX_CPU_PERCENT,
        )
        cpu_monitor_task = asyncio.create_task(_monitor_cpu_limit())
    else:
        logger.info(
            "Process resource guard disabled (set ENABLE_PROCESS_RESOURCE_GUARD=true to enable)"
        )
        cpu_monitor_task = None
    yield
    _mark_shutdown()
    if cpu_monitor_task is not None:
        cpu_monitor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cpu_monitor_task
    if _previous_sigterm_handler is not None:
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, _previous_sigterm_handler)
    if _previous_sigint_handler is not None:
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGINT, _previous_sigint_handler)
    await close_pool()
