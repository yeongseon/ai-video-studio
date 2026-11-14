from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from creator_domain.exceptions import ServiceError
from creator_domain.models import RenderPlan
from creator_service.actionable_errors import failure_summary_from_code
from creator_service.ffmpeg_service import FFmpegService
from creator_service.render_profile import RenderProfile
from PIL import Image
from tasks import render_video, task_runner
from tasks.legacy_render_adapter import LegacyRenderInputs
from worker_loop import run_in_worker_loop
from .render_output_parity_support import render_through_production_adapters

from .terminal_validation_support import RunnerCase, runner_case as runner_case  # noqa: F401


@pytest.mark.parametrize("source", ["legacy", "timeline"])
@pytest.mark.parametrize("runner_case", ["memory"], indirect=True)
def test_real_render_failure_reaches_safe_task_failure_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner_case: RunnerCase, source: str,
) -> None:
    # Given an eligible run and real FFmpeg, with only input/approval services isolated.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1")
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    monkeypatch.setattr(render_video, "_ARTIFACT_ROOT", str(tmp_path))
    image = tmp_path / "image.png"
    Image.new("RGB", (64, 64), "white").save(image)
    plan = RenderPlan.model_validate({
        "segments": [{"kind": "image", "source": str(image), "duration_seconds": 1,
                      "timeline_start_seconds": 0}],
        "output_spec": {"width": 64, "height": 64, "fps": 30},
        "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 23},
    })
    profile = RenderProfile(width=64, height=64, preset="ultrafast")
    inputs = LegacyRenderInputs(plan, FFmpegService(profile), None, None, 1)

    async def prepared(*args):
        return inputs

    async def absent(*args):
        return None

    async def authorized(*args):
        return True

    async def timeline(ctx, selected_profile, output, *, layers):
        from tasks.timeline_render import TimelineFFmpegService
        from creator_service.ffmpeg_service import RenderInput
        TimelineFFmpegService(plan, profile).render(RenderInput([image], None, None, [1]), output)
        return 1

    monkeypatch.setattr(render_video, "prepare_legacy_render", prepared)
    monkeypatch.setattr(render_video, "authorize_owned_provider_attempt", authorized)
    monkeypatch.setattr(render_video._audio_service, "get_latest", absent)
    monkeypatch.setattr(render_video._subtitle_service, "get_latest", absent)
    monkeypatch.setattr(render_video, "render_saved_timeline", timeline)
    case = runner_case
    config = replace(case.config, task_name="render_video")

    async def execute(ctx):
        ctx.run["metadata"] = {"render_source": "timeline"} if source == "timeline" else {}
        return await render_video.execute_render(ctx)

    # When the real task execution crosses the cap, then the typed failure survives wrapping.
    with pytest.raises(ServiceError) as raised:
        task_runner.run_task(case.delivery(), case.run_id, config, execute)
    assert type(raised.value).__name__ == "RenderScratchLimitExceeded"
    saved = run_in_worker_loop(case.runs.storage.get_run(case.run_id))
    assert (saved["current_stage"], saved["status"]) == ("FAILED", "failed")
    tracked = run_in_worker_loop(case.tracking.list_run_tasks(case.run_id))[0]
    assert tracked.status == "failed"
    summary = failure_summary_from_code(tracked.error_code)
    assert summary is not None and summary["category"] == "RENDER_SCRATCH_LIMIT"
    assert summary["retryable"] is True
    assert list((tmp_path / str(case.run_id) / "render").iterdir()) == []


def test_real_saved_timeline_counts_materialized_sources_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an approved Timeline already rendered through the production adapters.
    import hashlib
    import anyio
    from creator_service.actionable_errors import build_run_failure_summary
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "0")
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    image = tmp_path / "scene.png"
    Image.effect_noise((96, 160), 100).convert("RGB").save(image)
    captions = tmp_path / "captions.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:00,900\nSynthetic\n")
    paths = anyio.run(render_through_production_adapters, tmp_path, monkeypatch, image, None, captions)
    digest = hashlib.sha256(paths.timeline.read_bytes()).digest()
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1")
    # When the same real Timeline runs under a tiny cap, then materialization is budgeted.
    with pytest.raises(ServiceError) as raised:
        anyio.run(render_video.execute_render, paths.timeline_context)
    assert build_run_failure_summary(raised.value)["category"] == "RENDER_SCRATCH_LIMIT"
    assert hashlib.sha256(paths.timeline.read_bytes()).digest() == digest
    assert not any(path.name.startswith(("timeline-", ".render_tmp_", "output.tmp.")) for path in tmp_path.rglob("*"))
