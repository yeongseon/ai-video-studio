"""SF-21: segment-driven renderer entry point + legacy image_paths adapter.

Builds the existing ``RenderInput`` (parallel image_paths / scene_durations /
scene_transitions lists) from ordered RenderSegments or a RenderPlan, so callers
can move toward segments without changing ``FFmpegService.render`` internals.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from creator_domain.models import RenderPlan, RenderSegment, RenderSegmentKind

from creator_service.ffmpeg_service import RenderInput, _run_ffmpeg
from creator_service.render_profile import AudioCodec, Codec, RenderProfile
from creator_service.media_integrity import MediaExpectation, validate_media
from creator_service.render_frame_budget import allocate_frames
from creator_service.render_scratch import create_render_scratch

_PROBE_TIMEOUT_SECONDS = 30
_RENDER_TIMEOUT_SECONDS = 180
_CONCAT_TIMEOUT_SECONDS = 300


def _fit_filter(profile: RenderProfile) -> str:
    """Scale-and-pad video filter fitting any source into the profile geometry."""
    return (
        f"scale={profile.width}:{profile.height}"
        f":force_original_aspect_ratio=decrease,"
        f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p,setsar=1"
    )


class UnsupportedSegmentKindError(ValueError):
    """Raised when a segment kind is not supported by the legacy image path."""


class SegmentSourceError(ValueError):
    """Raised when a segment's source media is missing, malformed, or out of bounds."""


def render_input_from_segments(
    segments: list[RenderSegment],
    *,
    audio_path: Path | None = None,
    subtitle_path: Path | None = None,
) -> RenderInput:
    """Adapt ordered image RenderSegments into a legacy RenderInput.

    Segments are ordered by ``timeline_start_seconds``. Only image segments are
    supported by this legacy (image_paths) path; a video segment is rejected
    explicitly rather than silently mis-rendered.
    """
    if not segments:
        raise ValueError("segments must not be empty")

    ordered = sorted(segments, key=lambda s: s.timeline_start_seconds)
    for seg in ordered:
        if seg.kind is not RenderSegmentKind.IMAGE:
            raise UnsupportedSegmentKindError(
                f"legacy image renderer does not support segment kind {seg.kind.value!r}"
            )

    image_paths = [Path(seg.source) for seg in ordered]
    scene_durations = [seg.duration_seconds for seg in ordered]

    transitions = [seg.transition for seg in ordered]
    scene_transitions = (
        [t if t is not None else "" for t in transitions]
        if any(t is not None for t in transitions)
        else None
    )

    return RenderInput(
        image_paths=image_paths,
        audio_path=audio_path,
        subtitle_path=subtitle_path,
        scene_durations=scene_durations,
        scene_transitions=scene_transitions,
    )


def render_input_from_plan(
    plan: RenderPlan,
    *,
    audio_path: Path | None = None,
    subtitle_path: Path | None = None,
) -> RenderInput:
    """Adapt a RenderPlan's segments and compiled layers into a RenderInput.

    ``audio_path`` / ``subtitle_path`` override the plan's relative layer paths;
    callers holding absolute artifact paths (which the domain model's
    narration_path/subtitle_path reject) pass them here.
    """
    return render_input_from_segments(
        plan.segments,
        audio_path=(
            audio_path
            if audio_path is not None
            else Path(plan.narration_path) if plan.narration_path else None
        ),
        subtitle_path=(
            subtitle_path
            if subtitle_path is not None
            else Path(plan.subtitle_path) if plan.subtitle_path else None
        ),
    )


def render_profile_from_plan(plan: RenderPlan) -> RenderProfile:
    """Map a RenderPlan's OutputSpec geometry and EncodingProfile into a RenderProfile.

    Bridges the generic domain value objects onto the FFmpegService-facing
    RenderProfile: OutputSpec drives width/height/fps, EncodingProfile drives
    codec/crf/preset. The domain codec values (e.g. "libx264"/"aac") match the
    RenderProfile Codec/AudioCodec enum values, so they map by value.
    """
    spec = plan.output_spec
    encoding = plan.encoding_profile
    return RenderProfile(
        name=encoding.name,
        width=spec.width,
        height=spec.height,
        fps=spec.fps,
        video_codec=Codec(encoding.video_codec.value),
        audio_codec=AudioCodec(encoding.audio_codec.value),
        crf=encoding.crf,
        preset=encoding.preset,
    )


def _probe_source_duration(source: Path) -> float:
    """Probe a source video's duration; raise if missing or malformed."""
    if not source.is_file():
        raise SegmentSourceError(f"segment source not found: {source}")
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise SegmentSourceError(f"failed to probe segment source: {source}") from error
    if proc.returncode != 0:
        raise SegmentSourceError(f"segment source is not valid media: {source}")
    try:
        parsed = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as error:
        raise SegmentSourceError(f"failed to parse media metadata: {source}") from error
    has_video = any(
        s.get("codec_type") == "video" for s in (parsed.get("streams") or [])
    )
    if not has_video:
        raise SegmentSourceError(f"segment source has no video stream: {source}")
    raw = (parsed.get("format") or {}).get("duration")
    try:
        return float(raw)
    except (TypeError, ValueError) as error:
        raise SegmentSourceError(f"segment source has no duration: {source}") from error


def render_video_segment(
    segment: RenderSegment,
    output_path: Path,
    *,
    profile: RenderProfile,
    frame_budget: int | None = None,
) -> Path:
    """Render a source-video segment to a self-contained ``.ts`` clip.

    Trims the source to ``[trim_start, trim_end]`` (defaulting to the segment
    duration), scales/pads it to the profile geometry, and re-encodes with the
    profile codec/quality. Source media is validated (exists, decodable, has a
    video stream, and the trim is within bounds). Missing source audio is not an
    error (explicit audio policy): the clip is rendered video-only.
    """
    if segment.kind is not RenderSegmentKind.VIDEO:
        raise ValueError(
            f"render_video_segment requires a video segment, got {segment.kind.value!r}"
        )

    source = Path(segment.source)
    source_duration = _probe_source_duration(source)

    trim_start = segment.trim_start_seconds if segment.trim_start_seconds is not None else 0.0
    trim_end = (
        segment.trim_end_seconds
        if segment.trim_end_seconds is not None
        else trim_start + segment.duration_seconds
    )
    if trim_start > source_duration + 1e-6 or trim_end > source_duration + 1e-6:
        raise SegmentSourceError(
            f"trim [{trim_start}, {trim_end}] exceeds source duration {source_duration}"
        )
    clip_duration = trim_end - trim_start

    vf = _fit_filter(profile)

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-ss",
        f"{trim_start:.3f}",
        "-t",
        f"{clip_duration:.3f}",
        "-i",
        str(source),
        "-an",
        "-vf",
        f"{vf},fps={profile.fps},tpad=stop_mode=clone:stop_duration={1 / profile.fps},setpts=PTS-STARTPTS",
        "-c:v",
        profile.video_codec.value,
        "-crf",
        str(profile.crf),
        "-preset",
        profile.preset,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(profile.fps),
        "-force_key_frames",
        "expr:eq(n,0)",
        "-frames:v", str(frame_budget if frame_budget is not None else round(segment.duration_seconds * profile.fps)),
        "-f", "mp4", str(output_path),
    ]
    result = _run_ffmpeg(cmd, timeout=_RENDER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SegmentSourceError(
            f"failed to render video segment {source}: {result.stderr[-500:]}"
        )
    return output_path


def render_image_segment(
    segment: RenderSegment,
    output_path: Path,
    *,
    profile: RenderProfile,
    frame_budget: int | None = None,
) -> Path:
    """Render an image segment to a self-contained ``.ts`` clip.

    Holds the still for ``duration_seconds``, scaling/padding it to the profile
    geometry, producing a clip concat-compatible with video segments.
    """
    if segment.kind is not RenderSegmentKind.IMAGE:
        raise ValueError(
            f"render_image_segment requires an image segment, got {segment.kind.value!r}"
        )
    source = Path(segment.source)
    if not source.is_file():
        raise SegmentSourceError(f"segment source not found: {source}")

    cmd = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-loop",
        "1",
        "-i",
        str(source),
        "-vf",
        f"{_fit_filter(profile)},fps={profile.fps}",
        "-c:v",
        profile.video_codec.value,
        "-crf",
        str(profile.crf),
        "-preset",
        profile.preset,
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(profile.fps),
        "-force_key_frames",
        "expr:eq(n,0)",
        "-frames:v", str(frame_budget if frame_budget is not None else round(segment.duration_seconds * profile.fps)),
        "-f", "mp4", str(output_path),
    ]
    result = _run_ffmpeg(cmd, timeout=_RENDER_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise SegmentSourceError(
            f"failed to render image segment {source}: {result.stderr[-500:]}"
        )
    return output_path


def render_plan(plan: RenderPlan, output_path: Path) -> Path:
    """Render a mixed image/video plan to a single video.

    Each segment is rendered to an individual ``.ts`` clip by kind (image or
    video) fitted to the plan geometry, then the clips are concatenated in
    timeline order via the concat demuxer. Image-only and video-only plans are
    supported as special cases of the same path.
    """
    if not plan.segments:
        raise ValueError("plan has no segments to render")

    profile = render_profile_from_plan(plan)
    tmp_dir = create_render_scratch(output_path, ".render_plan_")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = output_path.with_suffix(f".tmp.{os.getpid()}.{os.urandom(4).hex()}.mp4")
    try:
        segment_paths: list[Path] = []
        budgets = allocate_frames([segment.duration_seconds for segment in plan.segments], profile.fps)
        for index, segment in enumerate(plan.segments):
            seg_path = tmp_dir / f"seg_{index:04d}.ts"
            if segment.kind is RenderSegmentKind.IMAGE:
                render_image_segment(segment, seg_path, profile=profile, frame_budget=budgets[index])
            elif segment.kind is RenderSegmentKind.VIDEO:
                render_video_segment(segment, seg_path, profile=profile, frame_budget=budgets[index])
            else:
                raise UnsupportedSegmentKindError(
                    f"unsupported segment kind: {segment.kind.value!r}"
                )
            segment_paths.append(seg_path)

        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text("".join(f"file '{p.name}'\nduration {budget / profile.fps:.12f}\n"
            for p, budget in zip(segment_paths, budgets, strict=True)))

        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            "-c:v",
            "copy",
            "-map",
            "0:v",
            str(tmp_output),
        ]
        result = _run_ffmpeg(cmd, timeout=_CONCAT_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise SegmentSourceError(
                f"failed to concatenate plan segments: {result.stderr[-500:]}"
            )
        validate_media(tmp_output, MediaExpectation(
            profile, plan.total_duration_seconds, bool(plan.narration_path or plan.music_path),
            len(plan.segments),
        ))
        tmp_output.rename(output_path)
        return output_path
    finally:
        tmp_output.unlink(missing_ok=True)
        for leftover in tmp_dir.glob("*"):
            leftover.unlink(missing_ok=True)
        tmp_dir.rmdir()
