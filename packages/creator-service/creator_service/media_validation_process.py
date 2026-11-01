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
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            break
        if signum == signal.SIGTERM:
            try:
                proc.wait(timeout=TERMINATION_GRACE)
            except subprocess.TimeoutExpired:
                continue
    proc.wait()


def run_validation(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Drain both pipes in fixed-size reads; budgets include all retained bytes.

    Finally kills the isolated group and reaps the direct child, without catching
    cancellation exceptions. SIGTERM gives cooperative parents time to reap their
    descendants before SIGKILL fences any remaining processes.
    """
    with subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as proc:
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
            try:
                returncode = proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise ValidationProcessError("validation_timeout") from exc
            return subprocess.CompletedProcess(cmd, returncode,
                buffers[0].decode("utf-8", errors="replace"), buffers[1].decode("utf-8", errors="replace"))
        finally:
            _terminate_group(proc)
