import os
import socket
import time
from pathlib import Path

import pytest

from creator_service.render_scratch_sweeper import SweepPolicy, SweepRuntime, sweep_render_scratch
from .test_render_scratch_sweep import named


def _runtime() -> SweepRuntime:
    return SweepRuntime(now=lambda: time.time() + 7200, monotonic=lambda: 0, hostname="local", pid_alive=lambda pid: False)


def test_newest_run_is_swept_when_root_has_10001_runs(tmp_path: Path) -> None:
    # Given more numeric run names than the old discovery cap.
    for run in range(1, 10002):
        (tmp_path / str(run)).mkdir()
    newest = tmp_path / "10001/render" / named()
    oldest = tmp_path / "1/render/.render_tmp_42_deadbeef"
    newest.mkdir(parents=True)
    oldest.mkdir(parents=True)
    # When a boot inspects its newest 1000 runs.
    result = sweep_render_scratch(tmp_path, SweepPolicy(), _runtime())
    # Then overflow never prevents reclaiming the newest run.
    assert not newest.exists()
    assert oldest.exists()
    assert result.bounds == ("artifact:max_run_directories",)


def test_sorted_directory_overflow_keeps_sweeping_other_locations(tmp_path: Path) -> None:
    # Given 10001 children with stale entries inside and outside the sorted prefix.
    root = tmp_path / "artifacts"
    render = root / "2/render"
    render.mkdir(parents=True)
    first = render / ".render_tmp_42_deadbeef"
    beyond = render / "timeline-zzzzzzzz"
    first.mkdir()
    beyond.mkdir()
    for index in range(9999):
        (render / f"published-{index:05}").touch()
    older = root / "1/render/.render_tmp_42_deadbeef"
    older.mkdir(parents=True)
    scratch = tmp_path / "scratch"
    configured = scratch / named()
    configured.mkdir(parents=True)
    # When the per-directory prefix, not the root entry budget, is exhausted.
    result = sweep_render_scratch(root, SweepPolicy(scratch_root=scratch, max_entries=20000), _runtime())
    # Then older runs and configured scratch still converge.
    assert not first.exists()
    assert beyond.exists()
    assert not older.exists()
    assert not configured.exists()
    assert result.bounds == ("artifact:directory_entries",)


def test_configured_root_is_swept_when_artifact_run_limit_is_hit(tmp_path: Path) -> None:
    # Given 1001 runs of published output and a dead configured owner.
    root = tmp_path / "artifacts"
    for run in range(1, 1002):
        render = root / str(run) / "render"
        render.mkdir(parents=True)
        (render / "output.mp4").write_bytes(b"published")
    scratch = tmp_path / "scratch"
    entry = scratch / named()
    entry.mkdir(parents=True)
    # When artifact discovery reaches its run limit.
    result = sweep_render_scratch(root, SweepPolicy(scratch_root=scratch), _runtime())
    # Then configured-mode scratch has already been reclaimed.
    assert not entry.exists()
    assert result.bounds == ("artifact:max_run_directories",)


@pytest.mark.parametrize("crowded", ["scratch", "artifact"])
def test_entry_budgets_are_independent_when_either_root_is_full(tmp_path: Path, crowded: str) -> None:
    # Given a published name consuming one root's budget and stale scratch in both.
    root = tmp_path / "artifacts"
    render = root / "1/render"
    scratch = tmp_path / "scratch"
    render.mkdir(parents=True)
    scratch.mkdir()
    parents = {"scratch": scratch, "artifact": render}
    (parents[crowded] / "!published").write_bytes(b"keep")
    entries = [parent / named() for parent in (scratch, render)]
    for entry in entries:
        entry.mkdir()
    # When each root has its own one-entry budget.
    result = sweep_render_scratch(root, SweepPolicy(scratch_root=scratch, max_entries=1), _runtime())
    # Then only the crowded root's scratch waits, and both bounds are reported once.
    assert (parents[crowded] / named()).exists()
    other = "artifact" if crowded == "scratch" else "scratch"
    assert not (parents[other] / named()).exists()
    assert result.examined == 2
    assert result.bounds == ("scratch:max_entries", "artifact:max_entries")


def test_global_removal_budget_stops_both_roots_after_scratch_first(tmp_path: Path) -> None:
    # Given eligible entries in both roots.
    root = tmp_path / "artifacts"
    artifact = root / "1/render" / named()
    scratch = tmp_path / "scratch"
    configured = scratch / named()
    artifact.mkdir(parents=True)
    configured.mkdir(parents=True)
    # When one global removal is admitted.
    result = sweep_render_scratch(root, SweepPolicy(scratch_root=scratch, max_removals=1), _runtime())
    # Then scratch wins and no artifact deletion is admitted.
    assert not configured.exists()
    assert artifact.exists()
    assert result.removed == 1
    assert result.bounds == ("max_removals",)


def test_runtime_hostname_is_captured_when_sweep_is_called(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a hostname changed since this module was imported and a gone owner.
    entry = tmp_path / "1/render" / named(host="changed-at-call")
    entry.mkdir(parents=True)
    monkeypatch.setattr(socket, "gethostname", lambda: "changed-at-call")

    def gone(pid: int, signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", gone)
    # When runtime defaults are constructed for this sweep.
    result = sweep_render_scratch(tmp_path, SweepPolicy(max_age_seconds=0, min_grace_seconds=0))
    # Then ownership uses the current hostname rather than an import-time snapshot.
    assert result.removed == 1
    assert not entry.exists()
