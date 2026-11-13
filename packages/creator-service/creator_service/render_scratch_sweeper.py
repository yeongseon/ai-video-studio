from __future__ import annotations

import socket
import time
import hashlib
import logging
import os
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from creator_service.render_scratch import CONFIGURED_SCRATCH_NAME, PARTIAL_MP4_NAME, SCRATCH_NAME_PATTERNS
from creator_service.render_scratch_cleanup import directory_fd, remove_scratch_entry

logger = logging.getLogger(__name__)


def pid_is_alive(pid: int) -> bool:
    if not 1 <= pid <= 4194304:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@dataclass(frozen=True, slots=True)
class SweepPolicy:
    max_age_seconds: float = 3600
    min_grace_seconds: float = 60
    minimum_max_age_seconds: float = 1320
    max_entries: int = 10000
    max_removals: int = 500
    max_run_directories: int = 1000
    budget_seconds: float = 20
    scratch_root: Path | None = None


@dataclass(frozen=True, slots=True)
class SweepRuntime:
    now: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    hostname: str = field(default_factory=socket.gethostname)
    pid_alive: Callable[[int], bool] = pid_is_alive


@dataclass(frozen=True, slots=True)
class SweepResult:
    examined: int = 0
    removed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    bounds: tuple[str, ...] = ()


def sweep_render_scratch(
    artifact_root: Path, policy: SweepPolicy, runtime: SweepRuntime = SweepRuntime(),
) -> SweepResult:
    now = runtime.now()
    started = runtime.monotonic()
    host = hashlib.sha256(runtime.hostname.encode()).hexdigest()[:12]
    maximum_age = policy.max_age_seconds
    if 0 < maximum_age < policy.minimum_max_age_seconds:
        maximum_age = policy.minimum_max_age_seconds
        logger.warning("Render scratch sweep maximum age clamped", extra={"max_age_seconds": maximum_age})
    examined = removed = runs = 0
    skipped: Counter[str] = Counter()
    bounds: list[str] = []

    def skip(reason: str, *, warning: bool = False) -> None:
        skipped[reason] += 1
        if warning and skipped[reason] == 1:
            logger.warning("Render scratch sweep skipped %s", reason)

    def bounded() -> bool:
        limits = (
            ("max_entries", examined >= policy.max_entries),
            ("max_removals", removed >= policy.max_removals),
            ("budget_seconds", runtime.monotonic() - started >= policy.budget_seconds),
        )
        for name, hit in limits:
            if hit and name not in bounds:
                bounds.append(name)
        return bool(bounds)

    def clean(parent: int, run_id: str | None) -> None:
        nonlocal examined, removed
        with os.scandir(parent) as entries:
            names = []
            for entry in entries:
                if runtime.monotonic() - started >= policy.budget_seconds:
                    bounds.append("budget_seconds")
                    return
                names.append(entry.name)
                if len(names) > 10000:
                    bounds.append("directory_entries")
                    return
        for name in sorted(names):
            if bounded():
                return
            examined += 1
            if not any(pattern.fullmatch(name) for pattern in SCRATCH_NAME_PATTERNS):
                skip("unrecognised")
                continue
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    skip("symlink", warning=True)
                    continue
                partial = PARTIAL_MP4_NAME.fullmatch(name) is not None
                if not (stat.S_ISREG(info.st_mode) if partial else stat.S_ISDIR(info.st_mode)):
                    skip("type")
                    continue
                age = max(0, now - max(info.st_mtime, info.st_ctime))
                old = maximum_age > 0 and age >= maximum_age
                owner = CONFIGURED_SCRATCH_NAME.fullmatch(name)
                gone = False
                if owner and owner["host"] == host and age >= policy.min_grace_seconds:
                    raw_pid = owner["pid"]
                    if len(raw_pid) <= 7 and 1 <= int(raw_pid) <= 4194304:
                        gone = not runtime.pid_alive(int(raw_pid))
                if not (old or gone):
                    skip("fresh_or_live")
                    continue
                remove_scratch_entry(parent, name, info)
                removed += 1
                logger.info("Render scratch entry removed", extra={"scratch_name": name, "run_id": run_id})
            except FileNotFoundError:
                skip("vanished")
            except OSError:
                skip("deletion_error", warning=True)

    try:
        with directory_fd(artifact_root.resolve()) as root_fd:
            with os.scandir(root_fd) as entries:
                names = []
                for entry in entries:
                    if runtime.monotonic() - started >= policy.budget_seconds:
                        bounds.append("budget_seconds")
                        break
                    names.append(entry.name)
                    if len(names) > 10000:
                        bounds.append("root_entries")
                        break
            names.sort(key=lambda name: (0, -int(name), name) if name.isascii() and name.isdecimal()
                       and len(name) <= 20 else (1, 0, name))
            for name in names:
                if bounded():
                    break
                if runs >= policy.max_run_directories:
                    bounds.append("max_run_directories")
                    break
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        skip("symlink", warning=True)
                        continue
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    runs += 1
                    with directory_fd(name, root_fd) as run_fd:
                        info = os.stat("render", dir_fd=run_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            skip("symlink", warning=True)
                            continue
                        with directory_fd("render", run_fd) as render_fd:
                            clean(render_fd, name)
                except FileNotFoundError:
                    skip("vanished")
                except OSError:
                    skip("directory_error", warning=True)
    except FileNotFoundError:
        skip("root_absent")
    except OSError:
        skip("root_error", warning=True)
    if policy.scratch_root is not None and not bounded():
        try:
            with directory_fd(policy.scratch_root) as scratch_fd:
                clean(scratch_fd, None)
        except FileNotFoundError:
            skip("root_absent")
        except OSError:
            skip("root_error", warning=True)
    bounded()
    for bound in bounds:
        logger.info("Render scratch sweep bound reached", extra={"bound": bound})
    logger.info("Render scratch sweep complete", extra={
        "examined": examined, "removed": removed, "skipped": dict(skipped),
        "elapsed": runtime.monotonic() - started,
    })
    return SweepResult(examined, removed, dict(skipped), tuple(bounds))
