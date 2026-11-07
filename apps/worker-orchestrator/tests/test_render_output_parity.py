from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import anyio
import pytest
from creator_domain.models import (
    OutputSpec,
)
from creator_service.media_integrity import MediaExpectation, validate_media
from creator_service.stage_review_service import (
    InMemoryStageReviewStorage,
    StageReviewService,
)
from tasks.render_materializer import RenderSourceError
from tasks.timeline_render import render_saved_timeline
from tasks.legacy_render_contract import build_render_plan
from tests.render_output_parity_support import (
    RenderedAdapterPaths,
    render_through_production_adapters,
)
from tests.render_output_probe_support import audio as _audio
from tests.render_output_probe_support import frame_luma as _frame_luma
from tests.render_output_probe_support import probe as _probe
from tests.render_output_probe_support import run_ffmpeg as _run
from tests.render_output_probe_support import video as _video

_DURATION: Final = 1.0
_FPS: Final = 30
_SAMPLE_RATE: Final = 48_000


def _portable_validate_media(path: Path, expectation: MediaExpectation) -> None:
    # macOS lacks waitid; CI/Linux still executes the complete #941 validator.
    if hasattr(os, "waitid"):
        validate_media(path, expectation)

@pytest.fixture(scope="module")
def rendered_paths(
    tmp_path_factory: pytest.TempPathFactory,
) -> RenderedAdapterPaths:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("creator_service.ffmpeg_service.validate_media", _portable_validate_media)
    root = tmp_path_factory.mktemp("render-output-parity")
    image = root / "scene.png"
    audio = root / "narration.wav"
    subtitles = root / "captions.ass"
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=white:size=96x160",
            "-frames:v",
            "1",
            str(image),
        ]
    )
    _run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={_SAMPLE_RATE}:duration={_DURATION}",
            "-af",
            "pan=stereo|c0=c0|c1=c0",
            str(audio),
        ]
    )
    subtitles.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,96,&H00000000,&H000000FF,&H00FFFFFF,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,10,10,300,1\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:00.90,Default,,0,0,0,,PARITY\n"
    )
    rendered = anyio.run(
        render_through_production_adapters,
        root,
        monkeypatch,
        image,
        audio,
        subtitles,
    )
    monkeypatch.undo()
    return rendered


def test_generated_script_and_saved_timeline_output_facts_match(
    rendered_paths: RenderedAdapterPaths,
) -> None:
    # Given: the same image, narration, subtitles, duration, and shorts_default profile
    # rendered through the generated-script and saved-Timeline adapters.
    legacy, timeline, profile = (
        rendered_paths.legacy,
        rendered_paths.timeline,
        rendered_paths.profile,
    )
    plan = build_render_plan([Path("scene.png")], [_DURATION], None, profile)
    probes = (_probe(legacy), _probe(timeline))

    # When: their real MP4 artifacts are inspected with ffprobe.
    videos = tuple(_video(probe) for probe in probes)
    audio = tuple(_audio(probe) for probe in probes)

    # Then: current container, stream, CFR, frame-budget, and audio facts are in parity.
    assert plan.output_spec == OutputSpec.short_vertical()
    assert plan.encoding_profile.crf == profile.crf == 18
    assert plan.encoding_profile.preset == profile.preset == "fast"
    assert all(probe.format.format_name.startswith("mov,mp4") for probe in probes)
    assert all(
        probe.format.duration == pytest.approx(_DURATION, abs=1 / _FPS)
        for probe in probes
    )
    assert all((video.codec_name, video.profile) == ("h264", "High") for video in videos)
    assert all(
        (video.width, video.height, video.pix_fmt) == (1080, 1920, "yuv420p")
        for video in videos
    )
    assert all(video.r_frame_rate == video.avg_frame_rate == "30/1" for video in videos)
    assert all(abs(video.nb_frames - round(_DURATION * _FPS)) <= 2 for video in videos)
    assert all(video.bit_rate > 0 for video in videos)
    assert all(
        (sound.codec_name, sound.sample_rate, sound.channels) == ("aac", _SAMPLE_RATE, 2)
        for sound in audio
    )
    assert rendered_paths.legacy_metadata == {
        "render_profile": "shorts_default",
        "storage_provider": "local",
        "storage_key": "1/render/output.mp4",
    }
    assert rendered_paths.timeline_metadata == {
        "render_profile": "shorts_default",
        "storage_provider": "local",
        "storage_key": "2/render/output.mp4",
    }


def test_generated_script_and_saved_timeline_burn_the_same_subtitle(
    rendered_paths: RenderedAdapterPaths,
) -> None:
    # Given/When: both paths burn the same visible caption over a white source.
    legacy, timeline = rendered_paths.legacy, rendered_paths.timeline
    legacy_luma = _frame_luma(legacy, subtitle_region=True)
    timeline_luma = _frame_luma(timeline, subtitle_region=True)

    # Then: each first frame contains dark subtitle pixels (not a platform-sensitive hash).
    assert legacy_luma < 245
    assert timeline_luma < 245


def test_divergence_generated_script_fades_while_saved_timeline_defaults_to_cut(
    rendered_paths: RenderedAdapterPaths,
) -> None:
    # Characterization owned by #922/#923: an absent transition reaches the legacy
    # profile's fade default, while saved-Timeline rendering substitutes an explicit cut.
    legacy, timeline = rendered_paths.legacy, rendered_paths.timeline
    legacy_luma = _frame_luma(legacy)
    timeline_luma = _frame_luma(timeline)

    assert legacy_luma + 20 < timeline_luma


def test_saved_timeline_rejects_stale_approved_revision(
    rendered_paths: RenderedAdapterPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the already-rendered production context is changed to claim a stale approval.
    stale_context = rendered_paths.timeline_context
    stale_context.run = {
        "metadata": {"render_source": "timeline", "render_timeline_revision": 2}
    }
    monkeypatch.setattr(
        "creator_service.timeline_service.timeline_service",
        rendered_paths.timeline_service,
    )
    monkeypatch.setattr(
        "creator_service.stage_review_service.stage_review_service",
        rendered_paths.review_service,
    )

    async def record_stale_approval() -> None:
        await rendered_paths.review_service.record_approval(
            rendered_paths.timeline_context.run_id,
            "TIMELINE_REVIEW",
            notes='{"timeline_id":"saved-timeline","revision":2}',
        )

    anyio.run(record_stale_approval)

    # When/Then: the production adapter rejects it before materialization or FFmpeg.
    with pytest.raises(RenderSourceError, match="revision changed after approval"):
        anyio.run(
            render_saved_timeline,
            stale_context,
            rendered_paths.profile,
            tmp_path / "stale.mp4",
        )


def test_saved_timeline_requires_persisted_approval(
    rendered_paths: RenderedAdapterPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the saved revision is current but its persisted approval is absent.
    monkeypatch.setattr(
        "creator_service.timeline_service.timeline_service",
        rendered_paths.timeline_service,
    )
    monkeypatch.setattr(
        "creator_service.stage_review_service.stage_review_service",
        StageReviewService(InMemoryStageReviewStorage()),
    )
    rendered_paths.timeline_context.run = {
        "metadata": {"render_source": "timeline", "render_timeline_revision": 1}
    }

    # When/Then: the production adapter fails before materialization or FFmpeg.
    with pytest.raises(RenderSourceError, match="valid persisted approval"):
        anyio.run(
            render_saved_timeline,
            rendered_paths.timeline_context,
            rendered_paths.profile,
            tmp_path / "unapproved.mp4",
        )
