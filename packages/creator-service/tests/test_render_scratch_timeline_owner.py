import hashlib
import os
import socket
from pathlib import Path

import pytest

from creator_service import render_scratch as scratch
from creator_service.render_scratch_cleanup import cleanup_delivery_scratch
from creator_service.render_scratch_sweeper import SweepPolicy, SweepRuntime, sweep_render_scratch


def test_legacy_timeline_allocation_records_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given legacy placement and a known delivery pid.
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    monkeypatch.setattr(scratch.os, "getpid", lambda: 42)
    output = tmp_path / "artifacts/1/render/output.mp4"
    # When two real directories are allocated.
    directories = [scratch.create_render_scratch(output, "timeline-") for _ in range(2)]
    # Then each has a unique private name carrying the owner beside the output.
    assert directories[0] != directories[1]
    for directory in directories:
        match = scratch.LEGACY_SCRATCH_NAME.fullmatch(directory.name)
        assert match is not None
        assert match["pid"] == "42"
        assert directory.parent == output.parent
        assert directory.stat().st_mode & 0o777 == 0o700
        assert sum(bool(pattern.fullmatch(directory.name)) for pattern in scratch.SCRATCH_NAME_PATTERNS) == 1


    root = tmp_path / "scratch"
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(root))
    monkeypatch.setattr(scratch.os, "getpid", lambda: 42)
    directory = scratch.create_render_scratch(output, "timeline-")
    match = scratch.CONFIGURED_SCRATCH_NAME.fullmatch(directory.name)
    assert match is not None
    assert match["host"] == hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
    assert match["run"] == hashlib.sha256(str(output.parent.resolve()).encode()).hexdigest()[:12]
    assert match["pid"] == "42"
    assert directory.parent == root
    assert directory.stat().st_mode & 0o777 == 0o700


def test_delivery_cleanup_removes_allocated_legacy_timeline_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an abandoned allocation, competing deliveries, old scratch and a symlink.
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    monkeypatch.setattr(scratch.os, "getpid", lambda: 42)
    root = tmp_path / "artifacts"
    render = root / "1/render"
    owned = scratch.create_render_scratch(render / "output.mp4", "timeline-")
    (owned / "source").write_bytes(b"owned")
    kept = [render / "timeline-43_deadbeef", render / "timeline-abcdefgh",
            render / ".render_tmp_43_deadbeef", root / "2/render/timeline-42_deadbeef"]
    for directory in kept:
        directory.mkdir(parents=True)
        (directory / "payload").write_bytes(b"sibling")
    published = render / "output.mp4"
    published.write_bytes(b"published")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "payload").write_bytes(b"outside")
    link = render / "timeline-42_12345678"
    link.symlink_to(victim, target_is_directory=True)
    # When the real delivery callable reclaims pid 42 in run 1.
    cleanup_delivery_scratch(root, (1, 42), None)
    # Then only its real allocation disappears; all protection boundaries hold.
    assert not owned.exists()
    assert all((directory / "payload").read_bytes() == b"sibling" for directory in kept)
    assert published.read_bytes() == b"published"
    assert link.is_symlink()
    assert (victim / "payload").read_bytes() == b"outside"


def test_timeline_sweep_keeps_legacy_names_age_only(tmp_path: Path) -> None:
    # Given old and new names, including a recent entry with an absent local pid.
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    old_format = render / "timeline-abcdefgh"
    stale = render / "timeline-42_deadbeef"
    fresh = render / "timeline-43_deadbeef"
    for directory in (old_format, stale, fresh):
        directory.mkdir()
        (directory / "payload").write_bytes(b"scratch")
    changed = max(directory.stat().st_ctime for directory in (old_format, stale, fresh))
    # Give only the fresh entry a future mtime without falsifying ctime.
    os.utime(fresh, (changed + 2000, changed + 2000))
    probes: list[int] = []

    def absent(pid: int) -> bool:
        probes.append(pid)
        return False

    # When startup sweeps after max age for old entries, but after grace for fresh.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(
        now=lambda: changed + 3601, pid_alive=absent,
    ))
    # Then both stale formats are swept; even an absent pid cannot reclaim fresh legacy.
    assert not stale.exists()
    assert not old_format.exists()
    assert (fresh / "payload").read_bytes() == b"scratch"
    assert result.examined == 3
    assert result.removed == 2
    assert probes == []
