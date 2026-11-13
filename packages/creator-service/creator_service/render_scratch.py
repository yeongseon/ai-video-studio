"""Private render scratch allocation; published MP4 staging stays beside its output."""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class RenderScratchConfigurationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"RENDER_SCRATCH_DIR {reason}; use an absolute, writable directory outside ARTIFACT_ROOT without symlinks")


def configured_scratch_root() -> Path | None:
    """Validate without writing, including for admission before any job output exists."""
    raw = os.environ.get("RENDER_SCRATCH_DIR")
    if not raw:
        return None
    root = Path(raw)
    if not root.is_absolute() or ".." in root.parts:
        raise RenderScratchConfigurationError("must be absolute and contain no '..' components")
    artifact = Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")).resolve()
    if root.is_relative_to(artifact):
        raise RenderScratchConfigurationError("must not be inside the artifact tree")
    for candidate in (*reversed(root.parents), root):
        if candidate.is_symlink():
            raise RenderScratchConfigurationError("must not traverse a symlink")
        if candidate.exists() and not candidate.is_dir():
            raise RenderScratchConfigurationError("must name a directory, not a file")
    existing = root
    while not existing.exists():
        existing = existing.parent
    if not os.access(existing, os.W_OK | os.X_OK):
        raise RenderScratchConfigurationError("is not writable/searchable by the worker")
    return root


def create_render_scratch(output: Path, prefix: str) -> Path:
    """Allocate one run/output-scoped directory (0700), preserving unset legacy names."""
    root = configured_scratch_root()
    if root is not None and root.is_relative_to(output.parent.resolve()):
        raise RenderScratchConfigurationError("must not be inside the published output directory")
    parent = root if root is not None else output.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        if root is not None:
            # Hashes keep host/run identity safe as a filename; pid/random fence each owner.
            host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
            run = hashlib.sha256(str(output.parent.resolve()).encode()).hexdigest()[:12]
            return Path(tempfile.mkdtemp(prefix=f"{prefix}{host}_{run}_{os.getpid()}_", dir=parent))
        if prefix == "timeline-":
            return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        directory = parent / f"{prefix}{os.getpid()}_{os.urandom(4).hex()}"
        directory.mkdir(mode=0o700)
        return directory
    except OSError as exc:
        if root is not None:
            raise RenderScratchConfigurationError(f"cannot create private scratch ({exc.strerror})") from exc
        raise


@contextmanager
def render_scratch(output: Path, prefix: str) -> Iterator[Path]:
    directory = create_render_scratch(output, prefix)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


@contextmanager
def audio_concat_list(output: Path) -> Iterator[Path]:
    """Keep the old system-temp file when unset; relocate only transient concat input."""
    if configured_scratch_root() is not None:
        with render_scratch(output, "audio-concat-") as directory:
            yield directory / "concat.txt"
    else:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as stream:
            path = Path(stream.name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)


def create_sfx_scratch(output: Path) -> Path:
    if configured_scratch_root() is None:
        return Path(tempfile.mkdtemp(prefix="sfx_"))
    return create_render_scratch(output, "sfx_")
