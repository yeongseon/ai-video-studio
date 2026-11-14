from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .media_upload_validation import (
    MediaUploadRejected,
    _detect_image_mime,
    _probe_image_dimensions,
)

_PROBE_TIMEOUT_SECONDS = 30
_FFPROBE_FORMAT_TO_MIME = {
    "mp4": "video/mp4", "webm": "video/webm", "matroska": "video/webm",
    "mov": "video/quicktime", "mp3": "audio/mpeg", "wav": "audio/wav",
    "ogg": "audio/ogg", "m4a": "audio/mp4",
}


@dataclass(frozen=True, slots=True)
class _ProbedMetadata:
    width: int | None
    height: int | None
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    width: int | None
    height: int | None
    duration_seconds: float | None
    mime_type: str | None
    size_bytes: int


class _ProbeStream(BaseModel):
    codec_type: str | None = None
    width: int | None = None
    height: int | None = None
    duration: str | float | None = None


class _ProbeFormat(BaseModel):
    format_name: str = ""
    duration: str | float | None = None


class _ProbeDocument(BaseModel):
    streams: list[_ProbeStream] | None = None
    format: _ProbeFormat | None = None


def _run_ffprobe(data: bytes | Path) -> _ProbeDocument:
    if isinstance(data, Path):
        return _run_ffprobe_path(data)
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe"
        probe_path.write_bytes(data)
        return _run_ffprobe_path(probe_path)


def _run_ffprobe_path(probe_path: Path) -> _ProbeDocument:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(probe_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        raise MediaUploadRejected("Failed to probe media file") from error
    if proc.returncode != 0:
        raise MediaUploadRejected("File content is not a recognized media file")
    try:
        return _ProbeDocument.model_validate_json(proc.stdout)
    except ValidationError as error:
        raise MediaUploadRejected("Failed to parse media metadata") from error


def probe_media_metadata(
    data: bytes | Path, *, kind: str, max_bytes: int | None = None,
) -> ProbeResult:
    """Probe image/video/audio bytes without changing the source media."""
    if kind not in ("image", "video", "audio"):
        raise ValueError(f"Unsupported probe kind: {kind!r}")
    size = len(data) if isinstance(data, bytes) else data.stat().st_size
    if not size:
        raise MediaUploadRejected("Empty upload")
    if max_bytes is not None and size > max_bytes:
        raise MediaUploadRejected("Upload exceeds maximum allowed size")
    if kind == "image":
        mime = _detect_image_mime(data)
        if mime is None:
            raise MediaUploadRejected("File content is not a recognized image")
        width, height = _probe_image_dimensions(data)
        return ProbeResult(width, height, None, mime, size)

    parsed = _run_ffprobe(data)
    matching = [stream for stream in (parsed.streams or []) if stream.codec_type == kind]
    if not matching:
        raise MediaUploadRejected(f"No {kind} stream found in upload")
    stream = matching[0]
    duration_raw = stream.duration
    if duration_raw is None and parsed.format is not None:
        duration_raw = parsed.format.duration
    try:
        duration = float(duration_raw) if duration_raw is not None else None
    except (TypeError, ValueError):
        duration = None
    if duration is not None and duration <= 0:
        duration = None
    return ProbeResult(stream.width, stream.height, duration, _ffprobe_mime(parsed, kind), size)


def _ffprobe_mime(parsed: _ProbeDocument, kind: str) -> str | None:
    fmt = parsed.format.format_name if parsed.format is not None else ""
    names = {part.strip() for part in fmt.split(",") if part.strip()}
    for format_name, mime in _FFPROBE_FORMAT_TO_MIME.items():
        if format_name in names and mime.startswith(f"{kind}/"):
            return mime
    return None


def _probe_media_metadata(data: bytes, *, kind: str) -> _ProbedMetadata:
    result = probe_media_metadata(data, kind=kind)
    return _ProbedMetadata(result.width, result.height, result.duration_seconds)
