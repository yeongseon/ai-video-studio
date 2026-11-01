"""Real subprocess resource boundaries for validation."""
import os
import signal
import selectors
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_service.media_integrity import MediaIntegrityError, _run


@pytest.mark.parametrize("pipe", [1, 2])
def test_rejects_oversized_validation_pipe(pipe: int) -> None:
    # Given / When / Then: large tool output must not be retained as successful results.
    code = f"import os; os.write({pipe}, b'x' * (8 * 1024 * 1024))"
    with pytest.raises(MediaIntegrityError):
        _run([sys.executable, "-c", code], 5)


@pytest.mark.parametrize("interrupt", [None, KeyboardInterrupt, SoftTimeLimitExceeded])
def test_validation_cleanup_reaps_descendant_group(tmp_path: Path, interrupt) -> None:
    # Given: direct child owns/reaps its SIGTERM-ignoring grandchild.
    ready = tmp_path / "ready"
    code = """import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
def stop(signum, frame):
    child.kill()
    child.wait()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
open(sys.argv[1], 'w').write(str(os.getpid()))
print('ready', flush=True)
while True: time.sleep(1)
"""
    real_popen = subprocess.Popen
    groups: list[int] = []

    def spawn(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        groups.append(proc.pid)
        return proc

    original_select = selectors.DefaultSelector.select
    def select(self, timeout=None):
        events = original_select(self, timeout)
        if events and interrupt is not None:
            raise interrupt()
        return events

    # When / Then: timeout or cancellation cannot leave an isolated process behind.
    error_type = MediaIntegrityError if interrupt is None else interrupt
    try:
        with patch("subprocess.Popen", side_effect=spawn), patch.object(selectors.DefaultSelector, "select", select), pytest.raises(error_type):
            _run([sys.executable, "-c", code, str(ready)], 1)
        with pytest.raises(ProcessLookupError):
            os.killpg(groups[0], 0)
    finally:
        for pgid in groups:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
