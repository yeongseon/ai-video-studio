from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest
from creator_service.ffmpeg_service import _run_ffmpeg


@pytest.mark.parametrize("interrupted", [False, True], ids=["success", "original-error"])
def test_final_wait_timeout_is_bounded_logged_once_and_preserves_outcome(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, interrupted: bool,
) -> None:
    # Given a real child with only the unproducible D-state wait failure injected.
    real_popen = subprocess.Popen
    processes = []
    waits = []

    def launch(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        real_communicate = proc.communicate
        real_wait = proc.wait
        first = True

        def stuck_wait(timeout=None):
            waits.append(timeout)
            real_wait(timeout=3)  # Reap the real stand-in even on the Red path.
            raise subprocess.TimeoutExpired("synthetic", 0 if timeout is None else timeout)

        def communicate(*args, **kwargs):
            nonlocal first
            if first:
                first = False
                if interrupted:
                    proc.wait = stuck_wait
                    raise KeyboardInterrupt("original")
                result = real_communicate(*args, **kwargs)
                proc.wait = stuck_wait
                return result
            raise subprocess.TimeoutExpired("synthetic", 1)

        proc.communicate = communicate
        return proc

    monkeypatch.setattr(subprocess, "Popen", launch)
    cmd = [sys.executable, "-c", "print('synthetic-private-argument')"]
    started = time.monotonic()
    # When cleanup cannot reap within its deadline, then the original outcome survives.
    with caplog.at_level(logging.ERROR):
        if interrupted:
            with pytest.raises(KeyboardInterrupt, match="original"):
                _run_ffmpeg(cmd, timeout=5)
        else:
            assert _run_ffmpeg(cmd, timeout=5).returncode == 0
    assert time.monotonic() - started < 5
    assert waits == [10]
    assert len(caplog.records) == 1
    assert str(processes[0].pid) in caplog.text
    assert os.path.basename(sys.executable) in caplog.text
    assert "synthetic-private-argument" not in caplog.text
    assert processes[0].stdout.closed and processes[0].stderr.closed


def test_interrupt_after_communicate_reaps_child_never_signals_freed_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a real child reaped by communicate immediately before an interrupt.
    real_popen = subprocess.Popen
    group_signals = []
    direct_signals = []

    def launch(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        real_communicate = proc.communicate
        first = True

        def communicate(*args, **kwargs):
            nonlocal first
            result = real_communicate(*args, **kwargs)
            if first:
                first = False
                raise KeyboardInterrupt("after-reap")
            return result

        proc.communicate = communicate
        monkeypatch.setattr(proc, "kill", lambda: direct_signals.append(proc.pid))
        return proc

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", lambda pid, sig: group_signals.append((pid, sig)))
    # When the interrupt escapes, then neither group nor direct signals target a freed pid.
    with pytest.raises(KeyboardInterrupt, match="after-reap"):
        _run_ffmpeg([sys.executable, "-c", "print('done')"], timeout=5)
    assert group_signals == []
    assert direct_signals == []
