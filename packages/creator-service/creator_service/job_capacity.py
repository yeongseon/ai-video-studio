"""Free-space admission for jobs that write media (render, image and audio tasks).

A job is refused *before it writes anything* when the filesystem that holds
``ARTIFACT_ROOT`` or the scratch (temp) filesystem has less free space than its
configured watermark (``MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES`` and
``MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES``; zero disables a check). The refusal is a
``FilesystemLowSpace``, a retryable ``StorageLowWatermark``: nothing is purged,
truncated or rewritten to make room, and the job can simply run again once space is freed.

This is an admission floor, not a reservation. A job admitted just above the floor can
still run out of space, and several jobs admitted at once each see the same free space;
bounding what one run may write is a separate concern.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

from creator_service.storage_policy import (
    ARTIFACT_FILESYSTEM,
    SCRATCH_FILESYSTEM,
    FilesystemLowSpace,
    StoragePolicy,
)
from creator_service.telemetry import get_meter

logger = logging.getLogger(__name__)

_DEFAULT_ARTIFACT_ROOT = "data/artifacts"
_REFUSAL_COUNTER = "worker.job.low_space_refusals"


def ensure_job_capacity(
    task_name: str,
    *,
    run_id: int | None = None,
    policy: StoragePolicy | None = None,
    artifact_root: str | os.PathLike[str] | None = None,
    scratch_root: str | os.PathLike[str] | None = None,
) -> None:
    """Raise ``FilesystemLowSpace`` if a filesystem this job writes to is below its watermark.

    The artifact filesystem is checked first. Arguments default to the deployment's own
    configuration (``StoragePolicy.from_env()``, ``ARTIFACT_ROOT`` and the temp directory).
    """
    policy = StoragePolicy.from_env() if policy is None else policy
    if artifact_root is None:
        artifact_root = os.getenv("ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)
    if scratch_root is None:
        scratch_root = tempfile.gettempdir()
    checks = (
        (ARTIFACT_FILESYSTEM, artifact_root, policy.artifact_disk_low_watermark_bytes),
        (SCRATCH_FILESYSTEM, scratch_root, policy.temp_disk_low_watermark_bytes),
    )
    for filesystem, root, required_bytes in checks:
        if required_bytes <= 0:
            continue
        measured_at = _nearest_existing_directory(root)
        free_bytes = _free_bytes(measured_at)
        if free_bytes is None:
            logger.warning(
                "Cannot measure free space; the job is admitted without this check",
                extra={
                    "task_name": task_name,
                    "run_id": run_id,
                    "filesystem": filesystem,
                    "measured": False,
                    "path": str(measured_at),
                },
            )
            continue
        if free_bytes < required_bytes:
            _record_refusal(
                task_name=task_name,
                run_id=run_id,
                filesystem=filesystem,
                path=measured_at,
                free_bytes=free_bytes,
                required_bytes=required_bytes,
            )
            raise FilesystemLowSpace(
                incoming_bytes=0,
                free_bytes=free_bytes,
                filesystem=filesystem,
                required_bytes=required_bytes,
            )


def _nearest_existing_directory(root: str | os.PathLike[str]) -> Path:
    """The directory whose filesystem would hold ``root``, without creating anything."""
    candidate = Path(root).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError:
        candidate = candidate.absolute()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent
    return candidate


def _free_bytes(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _record_refusal(
    *,
    task_name: str,
    run_id: int | None,
    filesystem: str,
    path: Path,
    free_bytes: int,
    required_bytes: int,
) -> None:
    logger.warning(
        "Refusing job: %s filesystem is below its free-space watermark",
        filesystem,
        extra={
            "task_name": task_name,
            "run_id": run_id,
            "filesystem": filesystem,
            "free_bytes": free_bytes,
            "required_bytes": required_bytes,
            "shortfall_bytes": required_bytes - free_bytes,
            "path": str(path),
        },
    )
    # Created per event, like the DLQ counters: get_meter() is a no-op until telemetry is
    # initialised, so an import-time instrument would stay a no-op in a forked worker.
    counter = get_meter(__name__).create_counter(
        _REFUSAL_COUNTER,
        unit="1",
        description="Jobs refused before writing because a filesystem was below its free-space watermark",
    )
    counter.add(1, {"filesystem": filesystem, "celery.task_name": task_name})
