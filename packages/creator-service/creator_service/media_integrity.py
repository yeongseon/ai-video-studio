"""Bounded final-media acceptance, shared by renderers and future chunk callers.

Timing tolerance is max(100 ms, two frames), independent of segment count.
Nominal FPS tolerance is 0.01; average-rate drift allows one rounded frame per
segment plus one container endpoint frame, not a percentage of total duration.
The plan remains authoritative even with -shortest: short narration must not
silently truncate visuals. Legacy narration-derived durations and AAC/MP3 tail
rounding fit this tolerance; Timeline narration is padded to its plan duration.
"""

from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .render_profile import AudioCodec, Codec, RenderProfile
from .media_validation_process import ValidationProcessError, run_validation

PROBE_TIMEOUT: Final = 15
TAIL_TIMEOUT: Final = 15
MIN_SIZE_BYTES: Final = 1024


class MediaIntegrityError(RuntimeError):
    """Render failure (not upload rejection); retains existing worker mapping."""

    def __init__(self, rule: str, *, durations: tuple[float, ...] = (), expected: float | None = None) -> None:
        self.rule = rule
        self.durations = durations
        self.expected = expected
        super().__init__(f"FFmpeg output integrity failed: {rule}")


@dataclass(frozen=True, slots=True)
class MediaExpectation:
    profile: RenderProfile
    duration_seconds: float
    audio_required: bool
    segment_count: int = 1

    @property
    def duration_tolerance(self) -> float:
        return max(0.1, 2 / self.profile.fps)


class ProbeStream(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", allow_inf_nan=False)
    codec_type: str
    codec_name: str
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    avg_frame_rate: str = "0/0"
    r_frame_rate: str = "0/0"
    duration: float = Field(gt=0)


class ProbeFormat(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", allow_inf_nan=False)
    duration: float = Field(gt=0)
    size: int = Field(gt=0)


class ProbeDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    streams: tuple[ProbeStream, ...]
    format: ProbeFormat


def _video_codec(codec: Codec) -> str:
    match codec:
        case Codec.H264:
            return "h264"
        case Codec.H265:
            return "hevc"
        case unreachable:
            assert_never(unreachable)


def _audio_codec(codec: AudioCodec) -> str:
    match codec:
        case AudioCodec.AAC:
            return "aac"
        case AudioCodec.MP3:
            return "mp3"
        case unreachable:
            assert_never(unreachable)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return run_validation(cmd, timeout)
    except ValidationProcessError as exc:
        raise MediaIntegrityError(exc.rule) from exc
    except OSError as exc:
        raise MediaIntegrityError("validation_unavailable") from exc


def validate_media(output: Path, expected: MediaExpectation) -> None:
    """Accept only profile-complete metadata plus decoded end-of-media samples.

    Probe fields are restricted (no packet/full-frame scan); tail decode seeks
    to the last second and emits frame hashes, proving actual samples exist.
    Both subprocesses have 15 s deadlines. A 1 KiB floor admits small static
    shorts unlike the legacy 10 KiB guess; probe size must equal filesystem size.
    """
    if (not math.isfinite(expected.duration_seconds) or expected.duration_seconds <= 0
            or expected.profile.fps <= 0 or expected.segment_count <= 0):
        raise MediaIntegrityError("invalid expected timing")
    try:
        size = output.stat().st_size
    except OSError as exc:
        raise MediaIntegrityError("missing output") from exc
    if size < MIN_SIZE_BYTES:
        raise MediaIntegrityError("output size")
    probe = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration:format=duration,size",
        "-of", "json", str(output),
    ], PROBE_TIMEOUT)
    if probe.returncode != 0 or probe.stderr.strip():
        raise MediaIntegrityError("probe error")
    try:
        document = ProbeDocument.model_validate_json(probe.stdout)
    except ValidationError as exc:
        raise MediaIntegrityError("invalid probe metadata") from exc
    video = [stream for stream in document.streams if stream.codec_type == "video"]
    audio = [stream for stream in document.streams if stream.codec_type == "audio"]
    if len(video) != 1:
        raise MediaIntegrityError("video stream presence")
    if expected.audio_required and len(audio) != 1:
        raise MediaIntegrityError("required audio stream presence")
    stream = video[0]
    if (stream.width, stream.height) != (expected.profile.width, expected.profile.height):
        raise MediaIntegrityError("video dimensions")
    try:
        fps = float(Fraction(stream.r_frame_rate))
        average_fps = float(Fraction(stream.avg_frame_rate))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise MediaIntegrityError("invalid frame rate") from exc
    if not math.isfinite(fps) or abs(fps - expected.profile.fps) > 0.01:
        raise MediaIntegrityError("video frame rate")
    if (not math.isfinite(average_fps) or average_fps <= 0
            or abs(average_fps - fps) * stream.duration > expected.segment_count + 1):
        raise MediaIntegrityError("average video frame rate")
    if stream.codec_name != _video_codec(expected.profile.video_codec):
        raise MediaIntegrityError("video codec family")
    if expected.audio_required and audio[0].codec_name != _audio_codec(expected.profile.audio_codec):
        raise MediaIntegrityError("audio codec family")
    durations = [document.format.duration, stream.duration]
    if expected.audio_required:
        durations.append(audio[0].duration)
    if any(abs(duration - expected.duration_seconds) > expected.duration_tolerance + 1e-6 for duration in durations):
        raise MediaIntegrityError("duration completeness", durations=tuple(durations), expected=expected.duration_seconds)
    if document.format.size != size:
        raise MediaIntegrityError("output size metadata")
    tail = _run([
        "ffmpeg", "-v", "error", "-nostdin", "-xerror", "-sseof", "-1",
        "-threads", "1", "-i", str(output), "-t", "2", "-map", "0:v:0",
        "-map", "0:a:0?", "-f", "framehash", "-",
    ], TAIL_TIMEOUT)
    if tail.returncode != 0 or tail.stderr.strip():
        raise MediaIntegrityError("end-of-media decode")
    decoded = {line.split(",", 1)[0].strip() for line in tail.stdout.splitlines()
               if line and not line.startswith("#") and "," in line}
    if "0" not in decoded or (expected.audio_required and "1" not in decoded):
        raise MediaIntegrityError("end-of-media samples missing")
