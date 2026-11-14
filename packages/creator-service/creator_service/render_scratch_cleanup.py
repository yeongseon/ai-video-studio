from __future__ import annotations

import hashlib
import logging
import os
import shutil
import socket
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import Final

from creator_service.confined_directory_deletion import unlink_direct_child

from creator_service.render_scratch import (
    CONFIGURED_SCRATCH_NAME,
    LEGACY_SCRATCH_NAME,
    PARTIAL_MP4_NAME,
)

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
TIMEOUT_MAX_ENTRIES: Final = 10000
logger = logging.getLogger(__name__)


@contextmanager
def directory_fd(path: Path | str, parent: int | None = None) -> Iterator[int]:
    descriptor = os.open(path, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def remove_scratch_entry(parent: int, name: str, info: os.stat_result) -> None:
    """Delete an inspected direct child, refusing a substituted directory symlink."""
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(name, dir_fd=parent)
    elif stat.S_ISREG(info.st_mode):
        unlink_direct_child(parent, name)


def cleanup_delivery_scratch(
    artifact_root: Path, delivery: tuple[int, int], scratch_root: Path | None,
) -> None:
    """Confine timeout reclamation to this host, pid, and output-directory identity."""
    run_id, pid = delivery
    if not 1 <= pid <= 4194304 or run_id < 1:
        return
    render = artifact_root / str(run_id) / "render"
    run_hash = hashlib.sha256(str(render.resolve()).encode()).hexdigest()[:12]
    host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
    started = time.monotonic()
    examined = 0
    warned: set[str] = set()
    bound_logged = False

    def report_bound() -> None:
        nonlocal bound_logged
        if not bound_logged:
            bound_logged = True
            logger.info("Render timeout cleanup entry bound reached", extra={"run_id": run_id})

    def warning(kind: str) -> None:
        if kind not in warned:
            warned.add(kind)
            logger.warning("Render timeout cleanup skipped entry", extra={"reason": kind, "run_id": run_id})

    def clean(parent: int, *, configured_only: bool) -> None:
        nonlocal examined
        with os.scandir(parent) as entries:
            for entry in islice(entries, TIMEOUT_MAX_ENTRIES):
                if examined >= TIMEOUT_MAX_ENTRIES or time.monotonic() - started >= 2:
                    report_bound()
                    return
                examined += 1
                configured = CONFIGURED_SCRATCH_NAME.fullmatch(entry.name)
                legacy = LEGACY_SCRATCH_NAME.fullmatch(entry.name)
                partial = PARTIAL_MP4_NAME.fullmatch(entry.name)
                owned = bool(configured and configured["host"] == host
                             and configured["run"] == run_hash and configured["pid"] == str(pid))
                if not configured_only:
                    local = legacy or partial
                    owned = owned or bool(local and local["pid"] == str(pid))
                if not owned:
                    continue
                try:
                    info = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        warning("symlink")
                        continue
                    if not (stat.S_ISREG(info.st_mode) if partial else stat.S_ISDIR(info.st_mode)):
                        continue
                    remove_scratch_entry(parent, entry.name, info)
                    logger.info("Render timeout scratch removed", extra={"scratch_name": entry.name, "run_id": run_id})
                except FileNotFoundError:
                    continue
                except OSError:
                    warning("deletion_error")
        if examined >= TIMEOUT_MAX_ENTRIES:
            report_bound()

    try:
        with directory_fd(artifact_root.resolve()) as root_fd:
            with directory_fd(str(run_id), root_fd) as run_fd:
                with directory_fd("render", run_fd) as render_fd:
                    clean(render_fd, configured_only=False)
    except FileNotFoundError:
        logger.debug("Render timeout directory absent")
    except OSError:
        logger.warning("Render timeout directory unavailable", extra={"run_id": run_id})
    if scratch_root is not None:
        try:
            with directory_fd(scratch_root) as scratch_fd:
                clean(scratch_fd, configured_only=True)
        except FileNotFoundError:
            logger.debug("Render timeout scratch root absent")
