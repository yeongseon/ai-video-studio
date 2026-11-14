from pathlib import Path

import pytest

from creator_service import render_scratch as scratch


@pytest.mark.parametrize("configured,prefix", [
    (False, ".render_tmp_"), (False, ".render_plan_"), (False, "timeline-"),
    (True, ".render_tmp_"), (True, ".render_plan_"), (True, "timeline-"),
    (True, "sfx_"), (True, "audio-concat-"),
])
def test_allocated_name_matches_exactly_one_pattern(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool, prefix: str,
) -> None:
    # Given the unchanged allocator in each supported layout.
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    if configured:
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(tmp_path / "scratch"))
    directory = scratch.create_render_scratch(tmp_path / "artifacts/1/render/output.mp4", prefix)
    # When recognition is applied to an actual allocated name.
    matches = [p for p in getattr(scratch, "SCRATCH_NAME_PATTERNS", ()) if p.fullmatch(directory.name)]
    # Then exactly one strict pattern owns that name.
    assert len(matches) == 1


@pytest.mark.parametrize("name", [
    "output.mp4", ".render_tmp_12_deadbeef.extra", ".render_plan_1_deadbee", "timeline-",
    "timeline-abcdefghij", "sfx_12_deadbeef", "output.tmp.12.deadbeef.mp4.old",
    "output.tmp.12.DEADBEEF.mp4", ".render_tmp_abcdef_abcdef_12_abcdefgh",
    ".render_tmp_12_deadbeef\n", "../.render_tmp_12_deadbeef",
])
def test_near_miss_is_not_recognised(name: str) -> None:
    # Given a name outside the allocator's grammar; when parsed; then it is rejected.
    assert not any(p.fullmatch(name) for p in getattr(scratch, "SCRATCH_NAME_PATTERNS", ()))


def test_partial_mp4_is_recognised() -> None:
    # Given FFmpeg's sibling staging name; when parsed; then exactly one pattern owns it.
    assert sum(bool(p.fullmatch("output.tmp.12.deadbeef.mp4"))
               for p in getattr(scratch, "SCRATCH_NAME_PATTERNS", ())) == 1
