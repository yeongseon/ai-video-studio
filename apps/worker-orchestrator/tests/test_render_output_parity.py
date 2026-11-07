from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Final, Literal

import anyio
import pytest
from creator_domain.models import (
    EncodingProfile,
    MediaAsset,
    MediaOrigin,
    MediaType,
    OutputSpec,
    RenderPlan,
    Timeline,
)
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.media_integrity import MediaExpectation, validate_media
from creator_service.render_profile import RenderProfile
from creator_service.render_segments import render_input_from_plan
from creator_service.timeline_compiler import compile_timeline_to_render_plan
from pydantic import BaseModel, ConfigDict
from tasks.legacy_render_contract import build_render_plan
from tasks.timeline_render import TimelineFFmpegService

_DURATION: Final = 1.0
_FPS: Final = 30
_SAMPLE_RATE: Final = 48_000


class _VideoStream(BaseModel):
    model_config = ConfigDict(frozen=True)

    codec_type: Literal["video"]
    codec_name: str
    profile: str
    width: int
    height: int
    pix_fmt: str
    r_frame_rate: str
    avg_frame_rate: str
    nb_frames: int
    bit_rate: int


class _AudioStream(BaseModel):
    model_config = ConfigDict(frozen=True)

    codec_type: Literal["audio"]
    codec_name: str
    sample_rate: int
    channels: int


class _Format(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_name: str
    duration: float


class _Probe(BaseModel):
    model_config = ConfigDict(frozen=True)

    streams: tuple[_VideoStream | _AudioStream, ...]
    format: _Format


class _AssetResolver:
    def __init__(self, asset: MediaAsset) -> None:
        self._asset = asset

    async def get_asset(self, asset_id: int, workspace_id: int) -> MediaAsset | None:
        if asset_id == self._asset.id and workspace_id == self._asset.workspace_id:
            return self._asset
        return None


def _portable_validate_media(path: Path, expectation: MediaExpectation) -> None:
    # macOS lacks waitid; CI/Linux still executes the complete #941 validator.
    if hasattr(os, "waitid"):
        validate_media(path, expectation)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )


def _probe(path: Path) -> _Probe:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    return _Probe.model_validate_json(result.stdout)


def _video(probe: _Probe) -> _VideoStream:
    return next(stream for stream in probe.streams if isinstance(stream, _VideoStream))


def _audio(probe: _Probe) -> _AudioStream:
    return next(stream for stream in probe.streams if isinstance(stream, _AudioStream))


def _frame_luma(path: Path, *, subtitle_region: bool = False) -> float:
    video_filter = "crop=400:180:(iw-400)/2:ih-480,scale=1:1,format=gray"
    if not subtitle_region:
        video_filter = "scale=1:1,format=gray"
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            video_filter,
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return float(result.stdout[0])


@pytest.fixture(scope="module")
def rendered_paths(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, RenderPlan, RenderProfile]:
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
    profile = RenderProfile.default()
    legacy_plan = build_render_plan([image], [_DURATION], None, profile)
    legacy = root / "legacy.mp4"
    FFmpegService(profile).render(
        render_input_from_plan(legacy_plan, audio_path=audio, subtitle_path=subtitles),
        legacy,
    )

    asset = MediaAsset(
        id=1,
        workspace_id=1,
        project_id=1,
        media_type=MediaType.IMAGE,
        origin=MediaOrigin.UPLOADED,
        storage_key=str(image),
        created_at=datetime.now(timezone.utc),
    )
    timeline = Timeline.model_validate(
        {
            "id": "saved-timeline",
            "project_id": 1,
            "revision": 1,
            "segments": [
                {
                    "id": "segment-1",
                    "scene_id": "scene-1",
                    "asset_id": 1,
                    "timeline_start_seconds": 0.0,
                    "duration_seconds": _DURATION,
                }
            ],
        }
    )
    plan = anyio.run(
        partial(
            compile_timeline_to_render_plan,
            timeline,
            workspace_id=1,
            output_spec=OutputSpec.short_vertical(),
            encoding_profile=EncodingProfile.from_render_profile(profile),
            asset_resolver=_AssetResolver(asset),
        )
    )
    timeline_output = root / "timeline.mp4"
    TimelineFFmpegService(plan, profile).render(
        RenderInput(
            image_paths=[image],
            audio_path=audio,
            subtitle_path=subtitles,
            scene_durations=[_DURATION],
            scene_transitions=[segment.transition or "cut" for segment in plan.segments],
        ),
        timeline_output,
    )
    monkeypatch.undo()
    return legacy, timeline_output, plan, profile


def test_generated_script_and_saved_timeline_output_facts_match(
    rendered_paths: tuple[Path, Path, RenderPlan, RenderProfile],
) -> None:
    # Given: the same image, narration, subtitles, duration, and shorts_default profile
    # rendered through the generated-script and saved-Timeline adapters.
    legacy, timeline, plan, profile = rendered_paths
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


def test_generated_script_and_saved_timeline_burn_the_same_subtitle(
    rendered_paths: tuple[Path, Path, RenderPlan, RenderProfile],
) -> None:
    # Given/When: both paths burn the same visible caption over a white source.
    legacy, timeline, _, _ = rendered_paths
    legacy_luma = _frame_luma(legacy, subtitle_region=True)
    timeline_luma = _frame_luma(timeline, subtitle_region=True)

    # Then: each first frame contains dark subtitle pixels (not a platform-sensitive hash).
    assert legacy_luma < 245
    assert timeline_luma < 245


def test_divergence_generated_script_fades_while_saved_timeline_defaults_to_cut(
    rendered_paths: tuple[Path, Path, RenderPlan, RenderProfile],
) -> None:
    # Characterization owned by #922/#923: an absent transition reaches the legacy
    # profile's fade default, while saved-Timeline rendering substitutes an explicit cut.
    legacy, timeline, _, _ = rendered_paths
    legacy_luma = _frame_luma(legacy)
    timeline_luma = _frame_luma(timeline)

    assert legacy_luma + 20 < timeline_luma
