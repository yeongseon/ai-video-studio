"""Real-process safety regressions for a delivery's polled scratch limit."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from creator_domain.exceptions import ServiceError
from creator_service.ffmpeg_service import FFmpegService, RenderInput, _run_ffmpeg
from creator_service.render_profile import RenderProfile


class StandInCompleted(Exception):
    """Stop before the real concat step after the stand-in finishes normally."""


class StandInRenderer(FFmpegService):
    def __init__(self, script: str) -> None:
        super().__init__(RenderProfile(width=64, height=64))
        self.script = script

    def _render_segment(self, image_path, duration, output_path, transition_override=None):
        _run_ffmpeg([sys.executable, "-c", self.script, str(output_path)], timeout=8)
        raise StandInCompleted()


def run_standin(root: Path, script: str) -> None:
    StandInRenderer(script).render(RenderInput([root / "input.png"], None, None, [1]), root / "output.mp4")


def _children() -> set[int]:
    return {int(raw) for raw in Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()}


def _process_state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0]
    except FileNotFoundError:
        return None


def _wait_for_zombie(pid: int, timeout: float = 3.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _process_state(pid)
        if state in (None, "Z"):
            return state
        time.sleep(0.01)
    return _process_state(pid)


@pytest.fixture
def subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        pytest.fail(f"test subreaper setup failed: errno={ctypes.get_errno()}")
    before = _children()
    yield before
    for pid in _children() - before:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    libc.prctl(36, 0, 0, 0, 0)


def test_limit_kills_term_ignoring_group_and_reaps_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subreaper: set[int],
) -> None:
    # Given a real writer and grandchild, both ignoring TERM, with inherited pipes.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    pids = tmp_path / "pids"
    script = f"""
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')
with open(sys.argv[1], 'wb', buffering=0) as stream:
    for _ in range(120):
        stream.write(b'x' * 1024)
        time.sleep(0.05)
"""
    started = time.monotonic()
    processes: list[subprocess.Popen[str]] = []
    popen = subprocess.Popen

    def observe(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", observe)
    # When the delivery crosses the cap, then kill/cleanup completes in a time bound.
    with pytest.raises(ServiceError) as raised:
        run_standin(tmp_path / "render", script)
    assert type(raised.value).__name__ == "RenderScratchLimitExceeded"
    assert raised.value.cap_bytes == 1024
    assert raised.value.observed_bytes > 1024
    assert processes[0].returncode == -signal.SIGKILL
    assert processes[0].stdout.closed and processes[0].stderr.closed
    assert time.monotonic() - started < 5
    direct, grandchild = map(int, pids.read_text().split())
    with pytest.raises(ProcessLookupError):
        os.kill(direct, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(direct, os.WNOHANG)
    try:
        os.kill(grandchild, 0)
    except ProcessLookupError:
        pytest.fail("test subreaper must own the grandchild until it is reaped")
    assert _wait_for_zombie(grandchild) == "Z"
    _, status = os.waitpid(grandchild, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert _children() <= subreaper
    assert list((tmp_path / "render").iterdir()) == []


def test_normal_completion_never_signals_the_already_reaped_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a runner whose child exits normally and is reaped by communicate().
    signalled: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, sig: int) -> None:
        signalled.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    # When the process finishes successfully.
    result = _run_ffmpeg([sys.executable, "-c", "print('done')"], timeout=5)
    # Then no group signal may target the freed pid, which the kernel could reuse.
    assert result.returncode == 0
    assert signalled == []


def test_limit_kills_a_term_ignoring_grandchild_after_the_direct_child_exits_on_term(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subreaper: set[int],
) -> None:
    # Given a direct child that exits on SIGTERM while its grandchild ignores it.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    pids = tmp_path / "pids"
    script = f"""
import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(20)'])
Path({str(pids)!r}).write_text(str(child.pid))
with open(sys.argv[1], 'wb', buffering=0) as stream:
    for _ in range(120):
        stream.write(b'x' * 1024)
        time.sleep(0.05)
"""
    # When the delivery crosses the cap.
    with pytest.raises(ServiceError) as raised:
        run_standin(tmp_path / "render", script)
    # Then the group SIGKILL still reaches the grandchild, which the test subreaper then reaps.
    assert type(raised.value).__name__ == "RenderScratchLimitExceeded"
    grandchild = int(pids.read_text())
    assert _wait_for_zombie(grandchild) == "Z"
    _, status = os.waitpid(grandchild, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    assert _children() <= subreaper


def test_boundary_allows_exact_cap_but_rejects_one_more_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given adjacent sizes, the exact cap must remain allowed, not >= rejected.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    script = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*SIZE); time.sleep(0.7)"
    # When the two boundary deliveries run, then only the one-byte excess fails.
    with pytest.raises(StandInCompleted):
        run_standin(tmp_path / "exact", script.replace("SIZE", "1024"))
    with pytest.raises(ServiceError) as raised:
        run_standin(tmp_path / "over", script.replace("SIZE", "1025"))
    assert type(raised.value).__name__ == "RenderScratchLimitExceeded"


def test_disabled_cap_allows_large_writer_and_setting_is_read_per_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the same writer and different settings on consecutive deliveries.
    script = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*1048576); time.sleep(0.7)"
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "0")
    # When disabled then enabled, then only the enabled delivery is refused.
    with pytest.raises(StandInCompleted):
        run_standin(tmp_path / "disabled", script)
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1")
    with pytest.raises(ServiceError):
        run_standin(tmp_path / "enabled", script)


@pytest.mark.parametrize("raw", ["-1", "abc", "1_024", "+1", "１２", "", "oversized"])
def test_invalid_setting_falls_back_with_one_safe_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str,
) -> None:
    # Given malformed operator input (large data is constructed, never parametrized).
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "9" * 5000 if raw == "oversized" else raw)
    script = "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*1024)"
    # When a delivery starts, then fallback allows it and logs exactly once without raw data.
    with pytest.raises(StandInCompleted):
        run_standin(tmp_path / "render", script)
    warnings = [record for record in caplog.records if "RENDER_SCRATCH_MAX_BYTES" in record.message]
    assert len(warnings) == 1
    assert "9" * 100 not in caplog.text


def test_concurrent_deliveries_measure_only_owned_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given concurrent deliveries with identical caps and different scratch sizes.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    script = "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*SIZE); time.sleep(0.8)"

    def outcome(size: int) -> str:
        try:
            run_standin(tmp_path / str(size), script.replace("SIZE", str(size)))
        except StandInCompleted:
            return "completed"
        except ServiceError as exc:
            return type(exc).__name__
        raise AssertionError("stand-in should stop before concat")

    # When both run together, then the small delivery survives the other's excess.
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(outcome, [512, 2048])) == ["completed", "RenderScratchLimitExceeded"]


def test_default_has_measurement_headroom_and_whitespace_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unset cap and a sparse file larger than the documented default.
    monkeypatch.delenv("RENDER_SCRATCH_MAX_BYTES", raising=False)
    script = "import sys,time; f=open(sys.argv[1],'wb'); f.truncate(2147483649); f.close(); time.sleep(0.7)"
    # When default then whitespace-wrapped operator configuration applies, both enforce.
    with pytest.raises(ServiceError) as raised:
        run_standin(tmp_path / "default", script)
    assert raised.value.cap_bytes == 2147483648
    assert raised.value.cap_bytes > 30 * 34_000_000
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", " 1024\t")
    with pytest.raises(ServiceError) as configured:
        run_standin(tmp_path / "configured", script)
    assert configured.value.cap_bytes == 1024
