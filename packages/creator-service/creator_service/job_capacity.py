"""Free-space admission for jobs that write media (render, image and audio tasks).

A job is refused *before it writes anything* when the filesystem that holds
``ARTIFACT_ROOT`` or the scratch (temp) filesystem has less free space than its
configured watermark (``MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES`` and
``MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES``; zero or less disables a check). The refusal is a
``FilesystemLowSpace``, a retryable ``StorageLowWatermark``: nothing is purged,
truncated or rewritten to make room, and a new job can run once space is freed.

The check itself never writes. A disabled check resolves nothing, the artifact
filesystem is checked before the scratch directory is even looked up, and the scratch
directory is resolved without ``tempfile.gettempdir()``, whose first call creates, writes
and deletes a probe file (and raises when the temp filesystem is full, which is exactly
when this check has to answer).

This is an admission floor, not a reservation. A job admitted just above the floor can
still run out of space, and several jobs admitted at once each see the same free space;
bounding what one run may write is a separate concern.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from creator_service.storage_policy import (
    ARTIFACT_FILESYSTEM,
    SCRATCH_FILESYSTEM,
    FilesystemLowSpace,
)
from creator_service.telemetry import get_meter

logger = logging.getLogger(__name__)

ARTIFACT_WATERMARK_ENV = "MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES"
# The same setting that guards upload staging: it describes the temp filesystem.
SCRATCH_WATERMARK_ENV = "MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES"

_DEFAULT_ARTIFACT_ROOT = "data/artifacts"
_TEMP_ENVIRONMENT_VARIABLES = ("TMPDIR", "TEMP", "TMP")
_TEMP_FALLBACK_DIRECTORIES = ("/tmp", "/var/tmp", "/usr/tmp")
_REFUSAL_COUNTER = "worker.job.low_space_refusals"


def _floor_from_env(name: str) -> int:
    raw = os.getenv(name, "0")
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer number of bytes, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class JobCapacityPolicy:
    """Free bytes each filesystem must keep before a media job may start (zero or less: off)."""

    artifact_low_watermark_bytes: int = 0
    scratch_low_watermark_bytes: int = 0

    @classmethod
    def from_env(cls) -> JobCapacityPolicy:
        # Only the two job floors are read: an unrelated upload-budget setting that cannot be
        # parsed must not become a dependency of every media job. A malformed floor is an error
        # that names its variable; it is never silently read as zero.
        return cls(
            artifact_low_watermark_bytes=_floor_from_env(ARTIFACT_WATERMARK_ENV),
            scratch_low_watermark_bytes=_floor_from_env(SCRATCH_WATERMARK_ENV),
        )


def ensure_job_capacity(
    task_name: str,
    *,
    run_id: int | None = None,
    policy: JobCapacityPolicy | None = None,
    artifact_root: str | os.PathLike[str] | None = None,
    scratch_root: str | os.PathLike[str] | None = None,
) -> None:
    """Raise ``FilesystemLowSpace`` if a filesystem this job writes to is below its watermark.

    The artifact filesystem is checked first. Arguments default to the deployment's own
    configuration (the two environment floors, ``ARTIFACT_ROOT`` and the temp directory).
    """
    policy = JobCapacityPolicy.from_env() if policy is None else policy
    if policy.artifact_low_watermark_bytes > 0:
        _admit(
            task_name,
            run_id,
            ARTIFACT_FILESYSTEM,
            partial(_artifact_root, artifact_root),
            policy.artifact_low_watermark_bytes,
        )
    if policy.scratch_low_watermark_bytes > 0:
        _admit(
            task_name,
            run_id,
            SCRATCH_FILESYSTEM,
            partial(_scratch_root, scratch_root),
            policy.scratch_low_watermark_bytes,
        )


def _artifact_root(explicit: str | os.PathLike[str] | None) -> str | os.PathLike[str]:
    return explicit if explicit is not None else os.getenv("ARTIFACT_ROOT", _DEFAULT_ARTIFACT_ROOT)


def _scratch_root(explicit: str | os.PathLike[str] | None) -> str | os.PathLike[str]:
    return explicit if explicit is not None else _default_scratch_directory()


def _admit(
    task_name: str,
    run_id: int | None,
    filesystem: str,
    resolve_root: Callable[[], str | os.PathLike[str]],
    required_bytes: int,
) -> None:
    measured_at: Path | None = None
    try:
        measured_at = _nearest_existing_directory(resolve_root())
        free_bytes = shutil.disk_usage(measured_at).free
    except OSError:
        logger.warning(
            "Cannot measure free space; the job is admitted without this check",
            extra={
                "task_name": task_name,
                "run_id": run_id,
                "filesystem": filesystem,
                "measured": False,
                "path": str(measured_at) if measured_at is not None else None,
            },
        )
        return
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


def _default_scratch_directory() -> Path:
    """The directory ``tempfile`` uses, found without ``tempfile.gettempdir()``'s write probe.

    Once the process has used ``tempfile`` its choice is cached and is returned as is. Before
    that, the candidates are tried in ``tempfile``'s own order and the first usable one wins;
    usable means an existing, searchable and writable directory, which is checked without
    creating anything. The current directory is ``tempfile``'s last resort.
    """
    if tempfile.tempdir is not None:
        return Path(tempfile.tempdir)
    candidates = [os.environ.get(name) for name in _TEMP_ENVIRONMENT_VARIABLES]
    candidates.extend(_TEMP_FALLBACK_DIRECTORIES)
    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return Path(candidate)
    return Path.cwd()


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
