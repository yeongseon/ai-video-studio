import os
import time
from pathlib import Path
from typing import Literal

import pytest
from typing_extensions import assert_never

import creator_service.render_scratch_sweeper as module

InvalidProc = Literal[
    "missing-stat", "truncated", "nonnumeric", "missing-btime", "malformed-btime",
    "truncated-btime", "missing-boot-stat", "zero-clock", "negative-clock", "ancient",
    "future", "negative-start", "negative-boot", "unicode", "missing-parenthesis",
    "pid-zero", "pid-negative", "pid-large",
]


def stat_line(comm: str = "python", ticks: str = "12345") -> str:
    return f"7 ({comm}) S " + "0 " * 18 + ticks + " 999\n"


@pytest.mark.parametrize("comm", ["python", "worker (render) with ) spaces"])
def test_start_epoch_when_proc_is_well_formed(tmp_path: Path, comm: str) -> None:
    # Given field 22 at 123.45 seconds after a known boot epoch.
    (tmp_path / "7").mkdir()
    (tmp_path / "7/stat").write_text(stat_line(comm))
    (tmp_path / "stat").write_text("cpu 1 2 3\nbtime 1700000000\nprocesses 5\n")
    assert hasattr(module, "process_start_time"), "missing conservative proc reader"
    # When reading with deterministic clock ticks; then the epoch is exact.
    assert module.process_start_time(7, proc_root=tmp_path, clock_ticks=100) == 1700000123.45


@pytest.mark.parametrize("case", [
    "missing-stat", "truncated", "nonnumeric", "missing-btime", "malformed-btime",
    "truncated-btime", "missing-boot-stat", "zero-clock", "negative-clock", "ancient",
    "future", "negative-start", "negative-boot", "unicode", "missing-parenthesis",
    "pid-zero", "pid-negative", "pid-large",
])
def test_unknown_start_when_proc_data_is_invalid(tmp_path: Path, case: InvalidProc) -> None:
    # Given one malformed or unavailable kernel input in an otherwise valid tree.
    (tmp_path / "7").mkdir()
    process = tmp_path / "7/stat"
    process.write_text(stat_line())
    boot = tmp_path / "stat"
    boot.write_text("btime 1700000000\n")
    clock = 100
    pid = 7
    match case:
        case "missing-stat":
            process.unlink()
        case "truncated":
            process.write_text("7 (python) S 0\n")
        case "nonnumeric":
            process.write_text(stat_line(ticks="invalid"))
        case "missing-btime":
            boot.write_text("cpu 1 2 3\n")
        case "malformed-btime":
            boot.write_text("btime invalid\n")
        case "truncated-btime":
            boot.write_text("btime\n")
        case "missing-boot-stat":
            boot.unlink()
        case "zero-clock":
            clock = 0
        case "negative-clock":
            clock = -100
        case "ancient":
            boot.write_text("btime 1\n")
        case "future":
            boot.write_text(f"btime {int(time.time()) + 172800}\n")
        case "negative-start":
            process.write_text(stat_line(ticks="-1"))
        case "negative-boot":
            boot.write_text("btime -1700000000\n")
        case "unicode":
            process.write_bytes(b"\xff")
        case "missing-parenthesis":
            process.write_text(stat_line().replace(")", ""))
        case "pid-zero":
            pid = 0
        case "pid-negative":
            pid = -1
        case "pid-large":
            pid = 4194305
        case unreachable:
            assert_never(unreachable)
    assert hasattr(module, "process_start_time"), "missing conservative proc reader"
    # When the reader cannot prove a plausible start; then it never raises.
    assert module.process_start_time(pid, proc_root=tmp_path, clock_ticks=clock) is None


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_unknown_start_when_sysconf_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception],
) -> None:
    # Given a platform that cannot provide SC_CLK_TCK.
    def unavailable(name: str) -> int:
        raise error("synthetic sysconf failure")

    monkeypatch.setattr(os, "sysconf", unavailable)
    assert hasattr(module, "process_start_time"), "missing conservative proc reader"
    # When default clock resolution is requested; then identity remains unknown.
    assert module.process_start_time(7, proc_root=tmp_path) is None


def test_default_start_when_current_linux_process_is_read() -> None:
    # Given Linux procfs; derive interpreter start independently using uptime.
    if not Path("/proc/self/stat").exists():
        pytest.skip("/proc/self/stat is unavailable")
    before = time.time()
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    ticks = int(Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19])
    interpreter_start = before - uptime + ticks / os.sysconf("SC_CLK_TCK")
    assert hasattr(module, "process_start_time"), "missing real Linux process identity"
    # When using the production defaults; then the start is plausible and recent enough.
    started = module.process_start_time(os.getpid())
    assert isinstance(started, float)
    assert interpreter_start - 5 <= started <= time.time()
