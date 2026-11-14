from pathlib import Path

import pytest

import creator_service.render_scratch_sweeper as module
from .test_render_scratch_sweep import named
from .test_render_scratch_sweep_overflow import _runtime


@pytest.mark.parametrize("location", ["scratch", "artifact"])
def test_junk_cannot_starve_recognised_scratch(tmp_path: Path, monkeypatch, location: str) -> None:
    # Given more lexically earlier junk names than the per-directory cap.
    root = tmp_path / "artifacts"
    root.mkdir()
    parent = tmp_path / "scratch" if location == "scratch" else root / "1/render"
    parent.mkdir(parents=True)
    for index in range(6):
        (parent / f"!junk{index}").touch()
    entry = parent / named()
    entry.mkdir()
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_DIRECTORY", 5)
    policy = module.SweepPolicy(scratch_root=parent if location == "scratch" else None)
    # When one startup sweeps the directory.
    result = module.sweep_render_scratch(root, policy, _runtime())
    # Then recognised scratch is reclaimed without retaining junk as candidates.
    assert not entry.exists()
    assert result.removed == result.examined == 1
    assert result.skipped[f"{location}:unrecognised"] == 6
    assert not result.bounds


def test_discovery_retains_only_bounded_selected_names(tmp_path: Path, monkeypatch) -> None:
    # Given numeric runs and recognised children beyond both selection caps.
    for run in range(12):
        parent = tmp_path / str(run) / "render"
        parent.mkdir(parents=True)
        for index in range(9):
            (parent / f"timeline-{index:08}").mkdir()
    retained: list[int] = []
    child_retained: list[int] = []
    original_push = module.heapq.heappush
    original_insert = module.bisect.insort

    def push(heap, item):
        original_push(heap, item)
        retained.append(len(heap))

    def insert(names, name):
        original_insert(names, name)
        child_retained.append(len(names))

    monkeypatch.setattr(module.heapq, "heappush", push)
    monkeypatch.setattr(module.bisect, "insort", insert)
    monkeypatch.setattr(module, "MAX_ENTRIES_PER_DIRECTORY", 4)
    # When bounded streaming selection visits a larger directory.
    result = module.sweep_render_scratch(tmp_path, module.SweepPolicy(max_run_directories=3), _runtime())
    # Then root selection storage never exceeds its cap, with capped children per run.
    assert retained and max(retained) <= 3
    assert child_retained and max(child_retained) <= 4
    assert result.removed == result.examined == 12
    assert set(result.bounds) == {"artifact:max_run_directories", "artifact:directory_entries"}
