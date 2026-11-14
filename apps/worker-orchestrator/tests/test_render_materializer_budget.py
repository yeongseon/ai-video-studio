from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest
from creator_domain.models import MediaAsset, RenderPlan
from creator_service.ffmpeg_service import _run_ffmpeg
from creator_service.object_storage import LocalStorageBackend
from creator_service.render_scratch import render_scratch
from creator_service.render_scratch_monitor import RenderScratchBudget, RenderScratchLimitExceeded, render_budget
from tasks.render_materializer import materialize_plan


@pytest.mark.parametrize("extra", [0, 1024, 1025], ids=["sources-only", "growth-at-cap", "runaway"])
def test_source_copy_credit_preserves_only_the_configured_growth_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: int,
) -> None:
    # Given one authorized source reused by two Timeline segments, larger than the cap.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    backend = LocalStorageBackend(str(tmp_path / "storage"))
    key = "workspaces/7/assets/source.mp4"
    backend.upload(key, b"s" * 2048, content_type="video/mp4")
    asset = MediaAsset.model_validate({
        "id": 1, "workspace_id": 7, "project_id": 11, "media_type": "VIDEO",
        "origin": "UPLOADED", "storage_key": key, "created_at": datetime.now(timezone.utc),
    })
    plan = RenderPlan.model_validate({
        "segments": [{"kind": "video", "source": key, "duration_seconds": 1,
                      "timeline_start_seconds": start} for start in (0, 1)],
        "output_spec": {"width": 64, "height": 64, "fps": 30},
        "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
    })
    with render_budget("timeline") as budget, render_scratch(tmp_path / "output.mp4", "timeline-") as directory:
        # When materialized, then the real runner accepts sources and only up to cap growth.
        local = materialize_plan(plan, [asset], directory, backend=backend, storage_provider="local")
        assert local.segments[0].source == local.segments[1].source
        script = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'x'*int(sys.argv[2]))"
        cmd = [sys.executable, "-c", script, str(directory / "narration.wav"), str(extra)]
        if extra > 1024:
            with pytest.raises(RenderScratchLimitExceeded) as raised:
                _run_ffmpeg(cmd, timeout=5)
            assert raised.value.cap_bytes == 1024
            assert raised.value.observed_bytes == 2048 + extra
        else:
            assert _run_ffmpeg(cmd, timeout=5).returncode == 0
        assert budget.baseline_bytes == 2048


def test_credit_does_not_exempt_generated_scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given credited source bytes alongside the same quantity of generated scratch.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    with render_budget("non-source") as budget:
        budget.register(tmp_path)
        (tmp_path / "source").write_bytes(b"s" * 2048)
        budget.add_baseline(2048)
        (tmp_path / "segment.ts").write_bytes(b"g" * 2048)
        # When checked, then credit does not exempt generated media.
        with pytest.raises(RenderScratchLimitExceeded) as raised:
            budget.check()
        assert raised.value.observed_bytes == 4096


def test_negative_credit_cannot_reduce_the_baseline() -> None:
    # Given a delivery with a valid source credit.
    budget = RenderScratchBudget(10, "negative")
    budget.add_baseline(20)
    # When a negative credit is supplied, then the baseline remains nonnegative and unchanged.
    with pytest.raises(ValueError):
        budget.add_baseline(-21)
    assert budget.baseline_bytes == 20


@pytest.mark.parametrize("credit", [True, 1.5, Path("source")], ids=["bool", "float", "path"])
def test_credit_accepts_only_a_byte_count(credit: object) -> None:
    # Given a delivery with a valid source credit.
    budget = RenderScratchBudget(10, "type")
    budget.add_baseline(20)
    # When anything but a plain int is supplied (a Path would be followed through symlinks, a bool is not a size).
    with pytest.raises(TypeError):
        budget.add_baseline(credit)
    # Then the baseline is unchanged.
    assert budget.baseline_bytes == 20
