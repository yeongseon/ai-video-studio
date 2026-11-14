import os
from pathlib import Path

import pytest

from creator_service.render_scratch_sweeper import SweepPolicy, SweepRuntime, sweep_render_scratch
from .test_render_scratch_sweep import named


@pytest.mark.parametrize("offset,alive,age,host,name,removed", [
    (-10, True, 61, "local", named(), False),
    (6, True, 61, "local", named(), True),
    (5, True, 61, "local", named(), False),
    (None, True, 61, "local", named(), False),
    (None, False, 61, "local", named(), True),
    (6, True, 59, "local", named(), False),
    (6, True, 61, "remote", named(), False),
    (6, True, 61, "local", ".render_tmp_7_deadbeef", False),
    (6, True, 61, "local", "timeline-abcdefgh", False),
    (6, True, 61, "local", named("1"), True),
], ids=["owner", "reused", "boundary", "unknown", "dead", "grace", "host", "legacy", "ownerless", "pid1"])
def test_owner_identity_when_pid_start_is_known(
    tmp_path: Path, offset: float | None, alive: bool, age: float,
    host: str, name: str, removed: bool,
) -> None:
    # Given configured scratch with a deliberately old mtime but current ctime.
    scratch = tmp_path / "scratch"
    entry = scratch / name
    entry.mkdir(parents=True)
    (entry / "payload").write_bytes(b"scratch")
    os.utime(entry, (1, 1))
    info = entry.stat()
    changed = max(info.st_mtime, info.st_ctime)
    assert "pid_start_time" in SweepRuntime.__dataclass_fields__, "missing injectable process identity"
    runtime = SweepRuntime(
        now=lambda: changed + age, hostname=host, pid_alive=lambda pid: alive,
        pid_start_time=lambda pid: None if offset is None else changed + offset,
    )
    # When the real filesystem is swept without age-based reclamation.
    result = sweep_render_scratch(tmp_path / "artifacts", SweepPolicy(
        scratch_root=scratch, max_age_seconds=0,
    ), runtime)
    # Then only a gone identity past grace is removed; ctime and slack protect owners.
    assert entry.exists() is not removed
    assert result.removed == int(removed)


def test_crash_restart_when_live_pid_seven_was_reused(tmp_path: Path) -> None:
    # Given pid 7's crash leftovers, a live sibling and published output.
    scratch = tmp_path / "scratch"
    crashed = scratch / named("7")
    sibling = scratch / named("8")
    for entry in (crashed, sibling):
        entry.mkdir(parents=True)
        (entry / "segment.ts").write_bytes(b"segment")
        os.utime(entry, (1, 1))
    output = tmp_path / "artifacts/1/render/output.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"published")
    changed = max(crashed.stat().st_ctime, sibling.stat().st_ctime)
    assert "pid_start_time" in SweepRuntime.__dataclass_fields__, "restart cannot distinguish reused pid"
    # When the next start has the same hostname and a later live pid 7.
    result = sweep_render_scratch(tmp_path / "artifacts", SweepPolicy(scratch_root=scratch), SweepRuntime(
        now=lambda: changed + 120, hostname="local", pid_alive=lambda pid: True,
        pid_start_time=lambda pid: changed + 10 if pid == 7 else changed - 10,
    ))
    # Then only the crashed delivery is reclaimed, well before maximum age.
    assert result.removed == 1
    assert not crashed.exists()
    assert (sibling / "segment.ts").read_bytes() == b"segment"
    assert output.read_bytes() == b"published"


@pytest.mark.parametrize("alive,offset,age,reason", [
    (False, None, 61, "owner_gone"),
    (True, 6, 61, "owner_pid_reused"),
    (True, None, 3600, "max_age"),
])
def test_removal_reason_when_entry_is_reclaimed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    alive: bool, offset: float | None, age: float, reason: str,
) -> None:
    # Given one eligible entry and its process identity.
    entry = tmp_path / "1/render" / named()
    entry.mkdir(parents=True)
    changed = entry.stat().st_ctime
    assert "pid_start_time" in SweepRuntime.__dataclass_fields__, "missing injectable process identity"
    # When reclamation logs its decision.
    with caplog.at_level("INFO"):
        sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(
            now=lambda: changed + age, hostname="local", pid_alive=lambda pid: alive,
            pid_start_time=lambda pid: None if offset is None else changed + offset,
        ))
    # Then the removal has one static reason without additional identity data.
    records = [r for r in caplog.records if r.message == "Render scratch entry removed"]
    assert len(records) == 1
    assert getattr(records[0], "reason") == reason
