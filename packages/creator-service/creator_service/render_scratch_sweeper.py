from __future__ import annotations

import socket
import time
import hashlib
import heapq
import logging
import os
import stat
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from creator_service.render_scratch import CONFIGURED_SCRATCH_NAME, PARTIAL_MP4_NAME, SCRATCH_NAME_PATTERNS
from creator_service.render_scratch_cleanup import directory_fd, remove_scratch_entry

logger = logging.getLogger(__name__)
MAX_ENTRIES_PER_DIRECTORY: Final = 10000


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
    hostname: str = field(default_factory=lambda: socket.gethostname())
    pid_alive: Callable[[int], bool] = pid_is_alive


@dataclass(frozen=True, slots=True)
class SweepResult:
    examined: int = 0
    removed: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    bounds: tuple[str, ...] = ()


def sweep_render_scratch(
    artifact_root: Path, policy: SweepPolicy, runtime: SweepRuntime | None = None,
) -> SweepResult:
    if runtime is None:
        runtime = SweepRuntime()
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
    root_examined: Counter[str] = Counter()

    def bound(name: str) -> None:
        if name not in bounds:
            bounds.append(name)

    def skip(reason: str, *, warning: bool = False) -> None:
        skipped[reason] += 1
        if warning and skipped[reason] == 1:
            logger.warning("Render scratch sweep skipped %s", reason)

    def bounded() -> bool:
        limits = (
            ("max_removals", removed >= policy.max_removals),
            ("budget_seconds", runtime.monotonic() - started >= policy.budget_seconds),
        )
        for name, hit in limits:
            if hit:
                bound(name)
        return any(hit for _, hit in limits)

    def clean(parent: int, run_id: str | None, root: str) -> None:
        nonlocal examined, removed
        with os.scandir(parent) as entries:
            names = []
            for entry in entries:
                if runtime.monotonic() - started >= policy.budget_seconds:
                    bound("budget_seconds")
                    break
                names.append(entry.name)
        if len(names) > MAX_ENTRIES_PER_DIRECTORY:
            bound(f"{root}:directory_entries")
        for name in sorted(names)[:MAX_ENTRIES_PER_DIRECTORY]:
            if bounded():
                return
            if root_examined[root] >= policy.max_entries:
                bound(f"{root}:max_entries")
                return
            examined += 1
            root_examined[root] += 1
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
        if root_examined[root] >= policy.max_entries:
            bound(f"{root}:max_entries")

    if policy.scratch_root is not None and not bounded():
        try:
            with directory_fd(policy.scratch_root) as scratch_fd:
                clean(scratch_fd, None, "scratch")
        except FileNotFoundError:
            skip("root_absent")
        except OSError:
            skip("root_error", warning=True)
    def clean_artifacts() -> None:
        nonlocal runs
        if bounded():
            return
        with directory_fd(artifact_root.resolve()) as root_fd:
            with os.scandir(root_fd) as entries:
                names = []
                for entry in entries:
                    if runtime.monotonic() - started >= policy.budget_seconds:
                        bound("budget_seconds")
                        break
                    names.append(entry.name)
            numeric = [name for name in names if name.isascii() and name.isdecimal() and len(name) <= 20]
            if len(numeric) > policy.max_run_directories:
                bound("artifact:max_run_directories")
            names = heapq.nlargest(policy.max_run_directories, numeric, key=lambda name: (int(name), name))
            for name in names:
                if bounded():
                    break
                if root_examined["artifact"] >= policy.max_entries:
                    bound("artifact:max_entries")
                    break
                if runs >= policy.max_run_directories:
                    bound("artifact:max_run_directories")
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
                            clean(render_fd, name, "artifact")
                except FileNotFoundError:
                    skip("vanished")
                except OSError:
                    skip("directory_error", warning=True)
    try:
        clean_artifacts()
    except FileNotFoundError:
        skip("root_absent")
    except OSError:
        skip("root_error", warning=True)
    bounded()
    for reached in bounds:
        logger.info("Render scratch sweep bound reached", extra={"bound": reached})
    logger.info("Render scratch sweep complete", extra={
        "examined": examined, "removed": removed, "skipped": dict(skipped),
        "elapsed": runtime.monotonic() - started,
    })
    return SweepResult(examined, removed, dict(skipped), tuple(bounds))
