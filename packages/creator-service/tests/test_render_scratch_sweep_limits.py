import importlib
import os
from pathlib import Path

import pytest

from creator_service.render_scratch_sweeper import SweepPolicy, SweepRuntime, pid_is_alive, sweep_render_scratch
from .test_render_scratch_sweep import named


@pytest.mark.parametrize("pid", [0, -1, 4194305, 10**30])
def test_invalid_pid_never_reaches_kill(pid: int, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an invalid pid and a forbidden OS boundary.
    def forbidden(*args):
        raise AssertionError("unsafe kill")
    monkeypatch.setattr(os, "kill", forbidden)
    # When probing liveness; then it is conservatively unknown/alive.
    assert pid_is_alive(pid)


@pytest.mark.parametrize("error,alive", [
    (ProcessLookupError, False), (PermissionError, True), (OSError, True),
])
def test_pid_probe_classifies_os_errors(error, alive: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the documented kill(0) outcomes.
    def fail(pid, signal):
        raise error()
    monkeypatch.setattr(os, "kill", fail)
    # When a valid pid is probed; then only ESRCH proves death.
    assert pid_is_alive(42) is alive


def test_floor_clamp_warns_and_preserves_live_entry(tmp_path: Path, caplog) -> None:
    # Given a live owner's entry beyond a too-small configured age but below the floor.
    path = tmp_path / "1/render" / named(str(os.getpid()))
    path.mkdir(parents=True)
    now = path.stat().st_ctime + 1000
    # When sweeping with a 10-second age and a 1320-second safety floor.
    result = sweep_render_scratch(tmp_path, SweepPolicy(max_age_seconds=10), SweepRuntime(
        now=lambda: now, hostname="local", pid_alive=lambda pid: True,
    ))
    # Then the entry survives and one clamp warning is emitted.
    assert result.removed == 0
    assert path.exists()
    assert sum("maximum age clamped" in r.message for r in caplog.records) == 1


def test_disabled_age_keeps_ownerless_but_reclaims_dead_owner(tmp_path: Path) -> None:
    # Given ancient ownerless and locally owned scratch in separate runs.
    legacy = tmp_path / "1/render/timeline-abcdefgh"
    dead = tmp_path / "2/render" / named()
    legacy.mkdir(parents=True)
    dead.mkdir(parents=True)
    now = dead.stat().st_ctime + 7200
    # When age reclamation is disabled.
    result = sweep_render_scratch(tmp_path, SweepPolicy(max_age_seconds=0), SweepRuntime(
        now=lambda: now, hostname="local", pid_alive=lambda pid: False,
    ))
    # Then only the same-host gone owner qualifies.
    assert result.removed == 1
    assert legacy.exists()
    assert not dead.exists()


def test_wall_budget_stops_before_deletion(tmp_path: Path) -> None:
    # Given a stale entry and a monotonic clock already at the budget on the next read.
    path = tmp_path / "1/render" / named()
    path.mkdir(parents=True)
    ticks = iter([0.0, *([20.0] * 100)])
    # When startup sweeping has exhausted its wall budget.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(
        now=lambda: path.stat().st_ctime + 7200, monotonic=lambda: next(ticks),
    ))
    # Then nothing is deleted and time exhaustion is reported.
    assert result.removed == 0
    assert "budget_seconds" in result.bounds
    assert path.exists()


def test_vanished_entry_is_not_deletion_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given another worker winning the delete between inspection and reclamation.
    path = tmp_path / "1/render/.render_tmp_42_deadbeef"
    path.mkdir(parents=True)
    module = importlib.import_module("creator_service.render_scratch_sweeper")
    original = module.remove_scratch_entry

    def race(parent, name, info):
        original(parent, name, info)
        raise FileNotFoundError()

    monkeypatch.setattr(module, "remove_scratch_entry", race)
    # When the losing sweeper reclaims the same entry.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(now=lambda: path.stat().st_ctime + 7200))
    # Then the benign race is counted as vanished rather than failed deletion.
    assert result.skipped == {"vanished": 1}
    assert result.removed == 0


def test_configured_root_and_artifact_symlink_are_supported(tmp_path: Path) -> None:
    # Given an operator artifact-root symlink and a separate configured scratch root.
    root = tmp_path / "artifacts"
    path = root / "1/render/.render_tmp_42_deadbeef"
    path.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    scratch = tmp_path / "scratch"
    entry = scratch / named()
    entry.mkdir(parents=True)
    # When the configured root is swept with the run tree.
    result = sweep_render_scratch(alias, SweepPolicy(scratch_root=scratch), SweepRuntime(
        now=lambda: entry.stat().st_ctime + 7200,
    ))
    # Then both direct-child scratch entries are reclaimed without removing the root alias.
    assert result.removed == 2
    assert alias.is_symlink()
    assert not entry.exists()


def test_wrong_types_are_kept(tmp_path: Path) -> None:
    # Given scratch-like names with the wrong filesystem types.
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    directory = render / "output.tmp.42.deadbeef.mp4"
    directory.mkdir()
    regular = render / ".render_tmp_42_deadbeef"
    regular.write_bytes(b"not scratch")
    # When age would otherwise qualify them; then both are preserved.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(now=lambda: regular.stat().st_ctime + 7200))
    assert result.removed == 0
    assert result.skipped["type"] == 2
    assert regular.read_bytes() == b"not scratch"
    assert directory.is_dir()


def test_entry_bound_prefers_sorted_names(tmp_path: Path) -> None:
    # Given reverse creation order, unrelated entries consume the budget too.
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    later = render / "timeline-zzzzzzzz"
    earlier = render / ".render_tmp_42_deadbeef"
    later.mkdir()
    earlier.mkdir()
    # When one entry is allowed; then lexical order wins over directory enumeration order.
    result = sweep_render_scratch(tmp_path, SweepPolicy(max_entries=1), SweepRuntime(
        now=lambda: later.stat().st_ctime + 7200,
    ))
    assert result.examined == result.removed == 1
    assert not earlier.exists()
    assert later.exists()


def test_non_numeric_runs_are_excluded(tmp_path: Path) -> None:
    # Given a nonnumeric directory created before numeric run 10.
    legacy = tmp_path / "aaa/render/.render_tmp_42_deadbeef"
    recent = tmp_path / "10/render/.render_tmp_42_deadbeef"
    legacy.mkdir(parents=True)
    recent.mkdir(parents=True)
    # When the opening budget allows both; then only the numeric run is inspected.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(
        now=lambda: recent.stat().st_ctime + 7200,
    ))
    assert result.removed == 1
    assert legacy.exists()
    assert not recent.exists()


@pytest.mark.parametrize("run_name", ["workspaces", "run\nforged", "１２", "1" * 21])
def test_invalid_run_names_preserve_stale_scratch(tmp_path: Path, run_name: str, caplog) -> None:
    # Given an over-age recognised entry outside the numeric run-id contract.
    entry = tmp_path / run_name / "render/.render_tmp_42_deadbeef"
    entry.mkdir(parents=True)
    # When startup discovers artifact runs.
    with caplog.at_level("INFO"):
        result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(
            now=lambda: entry.stat().st_ctime + 7200,
        ))
    # Then neither deletion nor a removal log may treat the name as a run id.
    assert entry.is_dir()
    assert result.examined == result.removed == 0
    assert not [record for record in caplog.records if hasattr(record, "run_id")]


def test_partial_mp4_symlink_survives(tmp_path: Path) -> None:
    # Given a recognised partial name that redirects to a published file.
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    published = render / "output.mp4"
    published.write_bytes(b"published")
    link = render / "output.tmp.42.deadbeef.mp4"
    link.symlink_to(published)
    # When the age rule qualifies; then the link and its target both survive.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), SweepRuntime(now=lambda: link.lstat().st_ctime + 7200))
    assert result.removed == 0
    assert link.is_symlink()
    assert published.read_bytes() == b"published"
