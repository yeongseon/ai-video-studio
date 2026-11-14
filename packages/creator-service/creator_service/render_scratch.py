"""Private render scratch allocation; published MP4 staging stays beside its output."""

from __future__ import annotations

import hashlib
import errno
import logging
import os
import re
import shutil
import socket
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Protocol
from .render_scratch_monitor import register_scratch

_CONFIGURATION_ERRNOS: Final = frozenset({
    errno.ENAMETOOLONG, errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.ELOOP, errno.EINVAL,
})
logger = logging.getLogger(__name__)

CONFIGURED_SCRATCH_NAME: Final = re.compile(
    r"\A(?:\.render_tmp_|\.render_plan_|timeline-|sfx_|audio-concat-)"
    r"(?P<host>[0-9a-f]{12})_(?P<run>[0-9a-f]{12})_(?P<pid>[0-9]+)_[a-z0-9_]{8}\Z"
)
LEGACY_SCRATCH_NAME: Final = re.compile(
    r"\A(?:\.render_tmp_|\.render_plan_)(?P<pid>[0-9]+)_[0-9a-f]{8}\Z"
)
LEGACY_TIMELINE_NAME: Final = re.compile(r"\Atimeline-[a-z0-9_]{8}\Z")
PARTIAL_MP4_NAME: Final = re.compile(r"\Aoutput\.tmp\.(?P<pid>[0-9]+)\.[0-9a-f]{8}\.mp4\Z")
SCRATCH_NAME_PATTERNS: Final = (
    CONFIGURED_SCRATCH_NAME, LEGACY_SCRATCH_NAME, LEGACY_TIMELINE_NAME, PARTIAL_MP4_NAME,
)


class ConcatWriter(Protocol):
    def write(self, text: str, /) -> int: ...
    def flush(self) -> None: ...


class RenderScratchConfigurationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"RENDER_SCRATCH_DIR {reason}; use an absolute, writable directory outside ARTIFACT_ROOT without symlinks")


def configured_scratch_root() -> Path | None:
    """Validate without writing, including for admission before any job output exists."""
    raw = os.environ.get("RENDER_SCRATCH_DIR")
    if not raw:
        return None
    if any(character in raw for character in ("\n", "\r", "\x00")):
        raise RenderScratchConfigurationError("must not contain newline or NUL characters")
    if not Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise RenderScratchConfigurationError("must be absolute and contain no '..' components")
    # POSIX normpath preserves exactly two leading slashes; Linux aliases them to '/'.
    root = Path(os.path.normpath("/" + raw.lstrip("/")))
    try:
        existing = _inspect_root(root)
        name_limit = os.pathconf(existing, "PC_NAME_MAX")
        if name_limit > 0 and any(len(os.fsencode(part)) > name_limit for part in root.parts):
            raise RenderScratchConfigurationError("contains a component exceeding the filesystem name limit")
        _reject_overlap(root, Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")))
        if not os.access(existing, os.W_OK | os.X_OK):
            raise RenderScratchConfigurationError("is not writable/searchable by the worker")
    except OSError as exc:
        _classify_configuration_error(exc)
        raise
    return root


def _classify_configuration_error(error: OSError) -> None:
    if error.errno in _CONFIGURATION_ERRNOS:
        raise RenderScratchConfigurationError(f"cannot inspect/create private scratch ({error.strerror})") from error


def _inspect_root(root: Path, *, create: bool = False) -> Path:
    existing = Path(root.anchor)
    for candidate in (*reversed(root.parents), root):
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            if not create:
                continue
            try:
                candidate.mkdir(mode=0o700)
            except FileExistsError:
                # Another worker may create it; adoption still requires all trust checks.
                info = os.lstat(candidate)
            else:
                info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode):
            raise RenderScratchConfigurationError("must not traverse a symlink")
        if not stat.S_ISDIR(info.st_mode):
            raise RenderScratchConfigurationError("must name a directory, not a file")
        if candidate == root:
            if info.st_uid != os.geteuid():
                raise RenderScratchConfigurationError("root must be owned by the worker uid")
            if info.st_mode & 0o022:
                raise RenderScratchConfigurationError("root must not be group/world writable")
        elif info.st_uid not in (0, os.geteuid()):
            raise RenderScratchConfigurationError("every existing ancestor must be owned by root or the worker uid")
        elif info.st_mode & 0o022 and not (
            info.st_mode & stat.S_ISVTX and info.st_uid in (0, os.geteuid())
        ):
            raise RenderScratchConfigurationError("ancestor write access requires a sticky root/worker-owned directory")
        existing = candidate
    return existing


def _directory_lineage(path: Path) -> list[tuple[tuple[int, int], tuple[str, ...]]]:
    lineage: list[tuple[tuple[int, int], tuple[str, ...]]] = []
    for candidate in (path, *path.parents):
        try:
            info = candidate.stat()
        except FileNotFoundError:
            continue
        lineage.append(((info.st_dev, info.st_ino), path.relative_to(candidate).parts))
    return lineage


def _reject_overlap(root: Path, retained: Path) -> None:
    # Shared inode identities also expose bind aliases; suffixes cover missing descendants.
    for root_id, root_tail in _directory_lineage(root):
        for retained_id, retained_tail in _directory_lineage(retained.resolve()):
            if root_id == retained_id and (
                root_tail[:len(retained_tail)] == retained_tail
                or retained_tail[:len(root_tail)] == root_tail
            ):
                raise RenderScratchConfigurationError("must not overlap the artifact tree or published output directory")


def create_render_scratch(output: Path, prefix: str) -> Path:
    """Allocate one run/output-scoped directory (0700), preserving unset legacy names."""
    root = configured_scratch_root()
    parent = root if root is not None else output.parent
    try:
        if root is not None:
            _reject_overlap(root, output.parent)
            _inspect_root(root, create=True)
            _reject_overlap(root, Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")))
            # Hashes keep host/run identity safe as a filename; pid/random fence each owner.
            host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
            run = hashlib.sha256(str(output.parent.resolve()).encode()).hexdigest()[:12]
            directory = Path(tempfile.mkdtemp(prefix=f"{prefix}{host}_{run}_{os.getpid()}_", dir=parent))
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                if stat.S_ISLNK(info.st_mode):
                    directory.unlink(missing_ok=True)
                else:
                    cleanup_render_scratch(directory, logger)
                raise RenderScratchConfigurationError("allocated directory must be a real worker-owned 0700 directory")
            return register_scratch(directory)
        parent.mkdir(parents=True, exist_ok=True)
        if prefix == "timeline-":
            return register_scratch(Path(tempfile.mkdtemp(prefix=prefix, dir=parent)))
        directory = parent / f"{prefix}{os.getpid()}_{os.urandom(4).hex()}"
        directory.mkdir(mode=0o700)
        return register_scratch(directory)
    except OSError as exc:
        if root is not None:
            _classify_configuration_error(exc)
        raise


def cleanup_render_scratch(directory: Path, log: logging.Logger) -> None:
    """Keep publication successful while reporting one path-redacted cleanup failure."""
    warned = False

    def report(_function: Callable[..., None], _path: str, error: BaseException) -> None:
        nonlocal warned
        if isinstance(error, FileNotFoundError) or warned:
            return
        warned = True
        log.warning("Render scratch cleanup failed", extra={
            "scratch_directory": directory.name,
            "errno": error.errno if isinstance(error, OSError) else None,
        })

    shutil.rmtree(directory, onexc=report)


@contextmanager
def render_scratch(output: Path, prefix: str) -> Iterator[Path]:
    directory = create_render_scratch(output, prefix)
    try:
        yield directory
    finally:
        cleanup_render_scratch(directory, logger)


@contextmanager
def audio_concat_list(output: Path) -> Iterator[tuple[Path, ConcatWriter]]:
    """Keep the old system-temp file when unset; relocate only transient concat input."""
    if configured_scratch_root() is not None:
        with render_scratch(output, "audio-concat-") as directory:
            path = directory / "concat.txt"
            with path.open("x") as stream:
                yield path, stream
    else:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as stream:
            path = Path(stream.name)
            register_scratch(path)
            try:
                yield path, stream
            finally:
                path.unlink(missing_ok=True)


def create_sfx_scratch(output: Path) -> Path:
    if configured_scratch_root() is None:
        return register_scratch(Path(tempfile.mkdtemp(prefix="sfx_")))
    return create_render_scratch(output, "sfx_")
