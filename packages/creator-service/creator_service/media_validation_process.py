"""Byte-bounded validation subprocesses with group cleanup on every exit."""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from typing import Final

STDOUT_LIMIT: Final = 1024 * 1024
STDERR_LIMIT: Final = 64 * 1024
READ_BYTES: Final = 8192
TERMINATION_GRACE: Final = 0.2


class ValidationProcessError(RuntimeError):
    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(rule)


def _terminate_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        _observe_exit(proc.pid, time.monotonic() + TERMINATION_GRACE)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            proc.wait(timeout=1)


def _observe_exit(pid: int, deadline: float) -> bool:
    """Observe without reaping: the zombie leader anchors PGID until escalation."""
    while True:
        if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))


def run_validation(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Drain both pipes in fixed-size reads; budgets include all retained bytes.

    Finally kills the isolated group and reaps the direct child, without catching
    cancellation exceptions. SIGTERM gives cooperative parents time to reap their
    descendants before SIGKILL fences any remaining processes.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    try:
            assert proc.stdout is not None and proc.stderr is not None
            buffers = [bytearray(), bytearray()]
            limits = [STDOUT_LIMIT, STDERR_LIMIT]
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, 0)
                selector.register(proc.stderr, selectors.EVENT_READ, 1)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValidationProcessError("validation_timeout")
                    for key, _ in selector.select(remaining):
                        index = 0 if key.fileobj is proc.stdout else 1
                        chunk = os.read(key.fd, READ_BYTES)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        if len(buffers[index]) + len(chunk) > limits[index]:
                            raise ValidationProcessError("validation_output_overflow")
                        buffers[index].extend(chunk)
            if not _observe_exit(proc.pid, deadline):
                raise ValidationProcessError("validation_timeout")
    finally:
        try:
            _terminate_group(proc)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
    return subprocess.CompletedProcess(cmd, proc.returncode,
        buffers[0].decode("utf-8", errors="replace"), buffers[1].decode("utf-8", errors="replace"))
