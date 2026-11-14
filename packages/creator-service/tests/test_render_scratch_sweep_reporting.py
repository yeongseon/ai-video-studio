from pathlib import Path

import pytest

from creator_service.render_scratch_sweeper import SweepPolicy, sweep_render_scratch


@pytest.mark.parametrize("missing", [False, True], ids=["errors", "absent"])
def test_both_roots_report_their_own_failure(tmp_path: Path, caplog, missing: bool) -> None:
    # Given two missing roots or two regular files where directories are required.
    artifact = tmp_path / "artifacts"
    scratch = tmp_path / "scratch"
    if not missing:
        artifact.touch()
        scratch.touch()
    # When each root fails independently.
    result = sweep_render_scratch(artifact, SweepPolicy(scratch_root=scratch))
    # Then counters and warn-once state identify both roots.
    reason = "root_absent" if missing else "root_error"
    assert result.skipped == {f"scratch:{reason}": 1, f"artifact:{reason}": 1}
    assert len(caplog.records) == (0 if missing else 2)
    if not missing:
        assert {record.args for record in caplog.records} == {("scratch:root_error",), ("artifact:root_error",)}


@pytest.mark.parametrize("location", ["scratch", "artifact"])
@pytest.mark.parametrize("count", [3, 4])
def test_entry_bound_requires_an_unexamined_candidate(tmp_path: Path, location: str, count: int) -> None:
    # Given exactly the root budget or one additional recognised entry.
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    parent = tmp_path / "scratch" if location == "scratch" else artifact / "1/render"
    parent.mkdir(parents=True)
    for index in range(count):
        (parent / f"timeline-{index:08}").mkdir()
    policy = SweepPolicy(max_entries=3, scratch_root=parent if location == "scratch" else None)
    # When all admitted names are examined without deleting fresh entries.
    result = sweep_render_scratch(artifact, policy)
    # Then only genuinely unexamined work reports entry exhaustion.
    assert result.examined == 3
    assert result.bounds == ((f"{location}:max_entries",) if count == 4 else ())
