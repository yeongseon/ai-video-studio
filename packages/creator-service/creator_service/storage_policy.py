from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass

from creator_domain.exceptions import ServiceError, ServiceUnavailableError

ARTIFACT_FILESYSTEM = "artifact"
SCRATCH_FILESYSTEM = "scratch"

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    max_stored_bytes: int = 0
    max_asset_count: int = 0
    max_reserved_bytes: int = 0
    # Free bytes the temp (scratch) filesystem must keep: upload staging checks it per upload,
    # and render/image/audio jobs check it before they start (job_capacity.ensure_job_capacity).
    temp_disk_low_watermark_bytes: int = 0
    # Free bytes the filesystem holding ARTIFACT_ROOT must keep before a job may start.
    artifact_disk_low_watermark_bytes: int = 0

    @classmethod
    def from_env(cls) -> StoragePolicy:
        return cls(
            max_stored_bytes=int(os.getenv("MEDIA_MAX_STORED_BYTES", "0")),
            max_asset_count=int(os.getenv("MEDIA_MAX_ASSET_COUNT", "0")),
            max_reserved_bytes=int(os.getenv("MEDIA_MAX_RESERVED_BYTES", "0")),
            temp_disk_low_watermark_bytes=int(
                os.getenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", "0")
            ),
            artifact_disk_low_watermark_bytes=int(
                os.getenv("MEDIA_ARTIFACT_DISK_LOW_WATERMARK_BYTES", "0")
            ),
        )

    def ensure_temp_capacity(self, incoming_bytes: int) -> None:
        if self.temp_disk_low_watermark_bytes <= 0:
            return
        free_bytes = shutil.disk_usage(tempfile.gettempdir()).free
        if free_bytes - incoming_bytes < self.temp_disk_low_watermark_bytes:
            raise StorageLowWatermark(incoming_bytes=incoming_bytes, free_bytes=free_bytes)


@dataclass(frozen=True, slots=True)
class StorageUsage:
    stored_bytes: int
    stored_asset_count: int
    reserved_bytes: int
    reserved_asset_count: int
    max_stored_bytes: int
    max_asset_count: int
    max_reserved_bytes: int


@dataclass(frozen=True, slots=True)
class StorageBudgetExceeded(ServiceError):
    workspace_id: int
    incoming_bytes: int
    stored_bytes: int
    reserved_bytes: int
    max_stored_bytes: int
    max_asset_count: int = 0
    max_reserved_bytes: int = 0

    http_status_code = 413

    def __str__(self) -> str:
        return "Workspace media storage budget exceeded"

    @property
    def detail(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class StorageLowWatermark(ServiceUnavailableError):
    incoming_bytes: int
    free_bytes: int

    def __str__(self) -> str:
        return "Temporary storage is below its configured low-watermark"

    @property
    def detail(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class FilesystemLowSpace(StorageLowWatermark):
    """A job was refused because a filesystem it writes to is below its free-space watermark.

    ``filesystem`` is ``ARTIFACT_FILESYSTEM`` (the filesystem that holds ARTIFACT_ROOT) or
    ``SCRATCH_FILESYSTEM`` (the temp filesystem). ``incoming_bytes`` is always 0: admission
    reserves nothing, it only refuses to start new work below the floor. The text names the
    storage and the shortfall and deliberately carries no path.
    """

    filesystem: str
    required_bytes: int

    def __str__(self) -> str:
        label = "Artifact storage" if self.filesystem == ARTIFACT_FILESYSTEM else "Scratch storage"
        shortfall = max(self.required_bytes - self.free_bytes, 0)
        return (
            f"{label} has {self.free_bytes // _MIB} MiB free but at least "
            f"{-(-self.required_bytes // _MIB)} MiB is required before a new job can start "
            f"({-(-shortfall // _MIB)} MiB short)"
        )

    def __reduce__(self) -> tuple[type[FilesystemLowSpace], tuple[int, int, str, int]]:
        # The generated __init__ takes keyword-friendly fields while BaseException.args stays
        # empty, so the default pickle path (cls(*args)) cannot rebuild it; Celery pickles
        # task exceptions between pool processes.
        return (
            type(self),
            (self.incoming_bytes, self.free_bytes, self.filesystem, self.required_bytes),
        )


def ensure_budget(
    *, workspace_id: int, incoming_bytes: int, usage: StorageUsage, policy: StoragePolicy,
) -> None:
    byte_limit_hit = (
        policy.max_stored_bytes > 0
        and usage.stored_bytes + usage.reserved_bytes + incoming_bytes > policy.max_stored_bytes
    )
    asset_limit_hit = (
        policy.max_asset_count > 0
        and usage.stored_asset_count + usage.reserved_asset_count + 1 > policy.max_asset_count
    )
    reservation_limit_hit = (
        policy.max_reserved_bytes > 0
        and usage.reserved_bytes + incoming_bytes > policy.max_reserved_bytes
    )
    if byte_limit_hit or asset_limit_hit or reservation_limit_hit:
        raise StorageBudgetExceeded(
            workspace_id=workspace_id,
            incoming_bytes=incoming_bytes,
            stored_bytes=usage.stored_bytes,
            reserved_bytes=usage.reserved_bytes,
            max_stored_bytes=policy.max_stored_bytes,
            max_asset_count=policy.max_asset_count,
            max_reserved_bytes=policy.max_reserved_bytes,
        )
