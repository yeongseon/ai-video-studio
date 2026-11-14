from __future__ import annotations

import errno
import logging
import os
from pathlib import Path

import pytest
from creator_service.render_scratch_monitor import RenderScratchBudget


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
