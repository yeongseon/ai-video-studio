from __future__ import annotations

import errno
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Self

import pytest
from creator_service.render_scratch_monitor import RenderScratchBudget


class FailingIterator:
    """A scandir stand-in whose iteration (not its open) raises."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Iterator[os.DirEntry[str]]:
        return self

    def __next__(self) -> os.DirEntry[str]:
        raise PermissionError(errno.EACCES, "injected iteration error")


@pytest.mark.parametrize("code", [errno.EACCES, errno.EMFILE, errno.ELOOP])
def test_registered_root_open_error_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    # Given a registered real scratch root whose open fails.
    budget = RenderScratchBudget(100, "root")
    budget.register(tmp_path)
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if path == tmp_path:
            raise OSError(code, "injected root error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    # When checked, then failure propagates rather than treating the root as empty.
    with pytest.raises(OSError) as raised:
        budget.check()
    assert raised.value.errno == code


def test_persistent_subtree_failure_warns_once_per_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a readable scratch root with a persistently unreadable subtree.
    (tmp_path / "locked").mkdir()
    (tmp_path / "readable").write_bytes(b"x" * 10)
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if os.fspath(path) == "locked":
            raise PermissionError(errno.EACCES, "injected subtree error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    budget = RenderScratchBudget(100, "subtree")
    budget.register(tmp_path)
    # When several complete passes encounter it, then only one warning is emitted.
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            budget.check()
    assert budget._total == 10
    assert len(caplog.records) == 1


def test_unreadable_root_fails_every_later_poll_not_every_other_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a registered root that stays unreadable.
    (tmp_path / "readable").write_bytes(b"x" * 10)
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if path == tmp_path:
            raise PermissionError(errno.EACCES, "injected root error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    budget = RenderScratchBudget(100, "sticky")
    budget.register(tmp_path)
    # When several consecutive polls run, then none reports a clean pass for a root it could not read.
    for _ in range(4):
        with pytest.raises(PermissionError):
            budget.check()


@pytest.mark.parametrize("seam", ["lstat", "scandir", "iteration"])
def test_registered_root_read_seams_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str) -> None:
    # Given a registered real root and a failure injected at one syscall seam of the root only.
    (tmp_path / "readable").write_bytes(b"x" * 10)
    real_lstat, real_scandir = Path.lstat, os.scandir

    def failing_lstat(self, *args, **kwargs):
        if self == tmp_path:
            raise PermissionError(errno.EACCES, "injected lstat error")
        return real_lstat(self, *args, **kwargs)

    def failing_scandir(target=None):
        if seam == "scandir":
            raise PermissionError(errno.EACCES, "injected scandir error")
        real_scandir(target).close()
        return FailingIterator()

    budget = RenderScratchBudget(100, seam)
    budget.register(tmp_path)
    # When checked, then the root is not treated as empty.
    with monkeypatch.context() as patch:
        if seam == "lstat":
            patch.setattr(Path, "lstat", failing_lstat)
        else:
            patch.setattr(os, "scandir", failing_scandir)
        with pytest.raises(PermissionError):
            budget.check()


def test_nested_iteration_error_is_skipped_with_one_warning_per_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a readable root whose only subdirectory cannot be iterated.
    (tmp_path / "locked").mkdir()
    (tmp_path / "readable").write_bytes(b"x" * 10)
    real_scandir = os.scandir
    opened = []

    def second_open_fails_to_iterate(target=None):
        opened.append(target)
        if len(opened) % 2 == 0:
            real_scandir(target).close()
            return FailingIterator()
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", second_open_fails_to_iterate)
    budget = RenderScratchBudget(100, "nested")
    budget.register(tmp_path)
    # When several passes meet it, then the pass still completes below the root and warns once.
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            budget.check()
    assert budget._total == 10
    assert len(caplog.records) == 1
