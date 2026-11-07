from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict


class VideoStream(BaseModel):
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


class AudioStream(BaseModel):
    model_config = ConfigDict(frozen=True)

    codec_type: Literal["audio"]
    codec_name: str
    sample_rate: int
    channels: int


class ProbeFormat(BaseModel):
    model_config = ConfigDict(frozen=True)

    format_name: str
    duration: float


class Probe(BaseModel):
    model_config = ConfigDict(frozen=True)

    streams: tuple[VideoStream | AudioStream, ...]
    format: ProbeFormat


def run_ffmpeg(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=True,
        text=True,
        timeout=60,
    )


def probe(path: Path) -> Probe:
    result = run_ffmpeg(
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
    return Probe.model_validate_json(result.stdout)


def video(probe_result: Probe) -> VideoStream:
    return next(
        stream for stream in probe_result.streams if isinstance(stream, VideoStream)
    )


def audio(probe_result: Probe) -> AudioStream:
    return next(
        stream for stream in probe_result.streams if isinstance(stream, AudioStream)
    )


def frame_luma(path: Path, *, subtitle_region: bool = False) -> float:
    video_filter = "crop=400:180:(iw-400)/2:ih-480,format=gray"
    if not subtitle_region:
        video_filter = "scale=1:1,format=gray"
    seek = ["-ss", "0.5"] if subtitle_region else []
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *seek,
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
    return float(min(result.stdout) if subtitle_region else result.stdout[0])
