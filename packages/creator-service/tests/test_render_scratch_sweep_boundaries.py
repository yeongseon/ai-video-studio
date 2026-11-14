import os
from pathlib import Path

import pytest

from creator_service.render_scratch_sweeper import SweepPolicy, SweepRuntime, sweep_render_scratch
from .test_render_scratch_sweep import named


@pytest.mark.parametrize("future", ["mtime", "ctime"])
def test_future_timestamp_counts_as_zero_age(tmp_path: Path, future: str) -> None:
    # Given an entry whose last-change timestamp lies ahead of the sweep clock.
    entry = tmp_path / "1/render" / named()
    entry.mkdir(parents=True)
    created = entry.stat().st_ctime
    now = created - 10 if future == "ctime" else created + 100
    if future == "mtime":
        os.utime(entry, (created + 200, created + 200))
    # When a short-grace dead-owner path sees the computed age.
    result = sweep_render_scratch(tmp_path, SweepPolicy(max_age_seconds=0, min_grace_seconds=0.1), SweepRuntime(
        now=lambda: now, hostname="local", pid_alive=lambda pid: False,
    ))
    # Then future time never becomes positive age or qualifies owner death.
    assert result.removed == 0
    assert entry.exists()


def test_repeated_directory_bounds_are_reported_and_logged_once(tmp_path: Path, monkeypatch, caplog) -> None:
    # Given two overflowing render directories and a smaller discovery limit.
    import creator_service.render_scratch_sweeper as module
    for run in (1, 2):
        render = tmp_path / str(run) / "render"
        render.mkdir(parents=True)
        (render / "timeline-aaaaaaaa").touch()
        (render / "timeline-bbbbbbbb").touch()
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_DIRECTORY", 1)
    # When both per-directory bounds are hit in one sweep.
    with caplog.at_level("INFO"):
        result = sweep_render_scratch(tmp_path, SweepPolicy())
    # Then a distinct root-qualified bound appears once in both surfaces.
    assert result.bounds == ("artifact:directory_entries",)
    assert [r.bound for r in caplog.records if hasattr(r, "bound")] == ["artifact:directory_entries"]


def test_global_removal_bound_stops_artifact_discovery(tmp_path: Path, monkeypatch) -> None:
    # Given scratch consuming the removal budget before artifact discovery.
    from creator_service.render_scratch_cleanup import directory_fd
    import creator_service.render_scratch_sweeper as module
    root = tmp_path / "artifacts"
    root.mkdir()
    scratch = tmp_path / "scratch"
    (scratch / named()).mkdir(parents=True)

    def confined(path, parent=None):
        assert path != root
        return directory_fd(path, parent)

    monkeypatch.setattr(module, "directory_fd", confined)
    # When the first root reaches the global removal cap.
    result = sweep_render_scratch(root, SweepPolicy(scratch_root=scratch, max_removals=1), SweepRuntime(
        now=lambda: scratch.stat().st_ctime + 7200,
    ))
    # Then the second root is never opened for discovery.
    assert result.bounds == ("max_removals",)
