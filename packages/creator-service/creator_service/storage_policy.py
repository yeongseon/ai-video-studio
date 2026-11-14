from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, fields

from creator_domain.exceptions import ServiceError, ServiceUnavailableError

ARTIFACT_FILESYSTEM = "artifact"
SCRATCH_FILESYSTEM = "scratch"

_MIB = 1024 * 1024

_Reduced = tuple[Callable[..., BaseException], tuple[type[BaseException], dict[str, object]]]


def _rebuild(cls: Callable[..., BaseException], values: dict[str, object]) -> BaseException:
    """Rebuild a dataclass exception by keyword, so a subclass keeps its own fields.

    The generated ``__init__`` takes keyword-friendly fields while ``BaseException.args`` holds only
    what was passed positionally, so the default pickle path (``cls(*args)``) cannot rebuild an
    instance created with keywords and drops a keyword mixed with positionals. Celery pickles task
    exceptions between pool processes.
    """
    return cls(**values)


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    max_stored_bytes: int = 0
    max_asset_count: int = 0
    max_reserved_bytes: int = 0
    # Free bytes the temp filesystem must keep: upload staging checks it per upload. The same
    # setting is the scratch floor of render/image/audio jobs (see job_capacity.JobCapacityPolicy).
    temp_disk_low_watermark_bytes: int = 0

    @classmethod
    def from_env(cls) -> StoragePolicy:
        return cls(
            max_stored_bytes=int(os.getenv("MEDIA_MAX_STORED_BYTES", "0")),
            max_asset_count=int(os.getenv("MEDIA_MAX_ASSET_COUNT", "0")),
            max_reserved_bytes=int(os.getenv("MEDIA_MAX_RESERVED_BYTES", "0")),
            temp_disk_low_watermark_bytes=int(
                os.getenv("MEDIA_TEMP_DISK_LOW_WATERMARK_BYTES", "0")
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


@dataclass(slots=True, eq=False)
class StorageBudgetExceeded(ServiceError):
    """Mutable exception state permits Starlette 1.10 traceback cleanup (#3205)."""

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

    def __reduce__(self) -> _Reduced:
        return _rebuild, (type(self), {f.name: getattr(self, f.name) for f in fields(self)})


@dataclass(slots=True, eq=False)
class StorageLowWatermark(ServiceUnavailableError):
    """Mutable exception state permits Starlette 1.10 traceback cleanup (#3205)."""

    incoming_bytes: int
    free_bytes: int

    def __str__(self) -> str:
        return "Temporary storage is below its configured low-watermark"

    @property
    def detail(self) -> str:
        return str(self)

    def __reduce__(self) -> _Reduced:
        return _rebuild, (type(self), {f.name: getattr(self, f.name) for f in fields(self)})


@dataclass(slots=True, eq=False)
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
