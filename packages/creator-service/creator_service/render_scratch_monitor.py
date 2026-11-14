"""Delivery-local accounting, independent of scratch placement and reclamation."""

from __future__ import annotations

import errno
import logging
import os
import stat
from collections.abc import Callable, Generator, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, assert_never

from creator_domain.exceptions import ServiceUnavailableError

logger = logging.getLogger(__name__)
DEFAULT_MAX_BYTES: Final = 2 * 1024**3
POLL_SECONDS: Final = 0.5
MAX_ENTRIES_PER_POLL: Final = 100_000


@dataclass(slots=True, eq=False)
class RenderScratchLimitExceeded(ServiceUnavailableError):
    """Mutable traceback state matches the service's other storage exceptions."""

    cap_bytes: int
    observed_bytes: int
    label: str

    def __str__(self) -> str:
        return "Render scratch exceeded its configured limit; existing outputs were preserved"

    @property
    def detail(self) -> str:
        return str(self)

    def __reduce__(self) -> tuple[type[RenderScratchLimitExceeded], tuple[int, int, str]]:
        return type(self), (self.cap_bytes, self.observed_bytes, self.label)


def _cap_from_env() -> int:
    raw = os.getenv("RENDER_SCRATCH_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_BYTES
    stripped = raw.strip()
    try:
        value = int(stripped) if stripped.isascii() and stripped.isdecimal() else -1
    except ValueError:
        value = -1
    if value >= 0:
        return value
    logger.warning("Invalid RENDER_SCRATCH_MAX_BYTES; using default")
    return DEFAULT_MAX_BYTES


def _sizes(paths: tuple[Path, ...], skipped: Callable[[OSError], None]) -> Generator[int, None, None]:
    """Yield once per entry; descriptor-relative no-follow opens fence symlink swaps."""
    for path in paths:
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                yield info.st_size
                continue
            yield 0
            if not stat.S_ISDIR(info.st_mode):
                continue
            with ExitStack() as resources:
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                resources.callback(os.close, descriptor)
                entries = resources.enter_context(os.scandir(descriptor))
                stack = [(descriptor, entries, resources)]
                while stack:
                    parent, iterator, owner = stack[-1]
                    try:
                        entry = next(iterator, None)
                    except OSError as error:
                        if len(stack) == 1:
                            raise
                        skipped(error)
                        entry = None
                    if entry is None:
                        stack.pop()
                        owner.close()
                        continue
                    try:
                        info = os.stat(entry.name, dir_fd=parent, follow_symlinks=False)
                        yield info.st_size if stat.S_ISREG(info.st_mode) else 0
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                            child_resources = ExitStack()
                            resources.callback(child_resources.close)
                            child_resources.callback(os.close, child)
                            stack.append((child, child_resources.enter_context(os.scandir(child)), child_resources))
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    except OSError as error:
                        skipped(error)
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue


@dataclass(slots=True)
class RenderScratchBudget:
    """Mutable registry and resumable pass total belonging to exactly one delivery."""

    cap_bytes: int
    label: str
    paths: list[Path] = field(default_factory=list)
    _walk: Generator[int, None, None] | None = None
    _total: int = 0
    baseline_bytes: int = field(default=0, init=False)
    _reported_skip: bool = field(default=False, init=False)

    def add_baseline(self, path_or_bytes: Path | int) -> None:
        """Credit a completed materialized source copy, on the delivery's owning thread."""
        match path_or_bytes:
            case Path():
                size = path_or_bytes.stat().st_size
            case int():
                size = path_or_bytes
            case unreachable:
                assert_never(unreachable)
        if size < 0:
            raise ValueError(size)
        self.baseline_bytes += size

    def _skipped(self, error: OSError) -> None:
        if not self._reported_skip:
            self._reported_skip = True
            logger.warning("Render scratch walk skipped an unreadable entry (%s)", errno.errorcode.get(error.errno or 0, "OSError"))

    def register(self, path: Path) -> None:
        if path not in self.paths:
            self.paths.append(path)

    def check(self) -> None:
        if self.cap_bytes == 0:
            return
        if self._walk is None:
            self._walk = _sizes(tuple(self.paths), self._skipped)
            self._total = 0
        for _ in range(MAX_ENTRIES_PER_POLL):
            try:
                size = next(self._walk, None)
            except OSError:
                self.close()
                raise
            if size is None:
                self.close()
                return
            self._total += size
            if self._total > self.cap_bytes + self.baseline_bytes:
                raise RenderScratchLimitExceeded(self.cap_bytes, self._total, self.label)

    def close(self) -> None:
        if self._walk is not None:
            self._walk.close()
            self._walk = None


current_budget: Final[ContextVar[RenderScratchBudget | None]] = ContextVar("render_scratch_budget", default=None)


def register_scratch(path: Path) -> Path:
    budget = current_budget.get()
    if budget is not None:
        budget.register(path)
    return path


@contextmanager
def render_budget(label: str) -> Iterator[RenderScratchBudget]:
    existing = current_budget.get()
    if existing is not None:
        yield existing
        return
    budget = RenderScratchBudget(_cap_from_env(), label)
    token = current_budget.set(budget)
    try:
        yield budget
    finally:
        budget.close()
        current_budget.reset(token)
