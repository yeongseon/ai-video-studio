from __future__ import annotations

import logging
import os
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx

from creator_provider.base import STTProvider, SubtitleResult
from creator_provider.exceptions import map_httpx_error


logger = logging.getLogger(__name__)


class WhisperSTTProvider(STTProvider):
    _ALLOWED_FORM_KEYS = frozenset(
        {
            "task",
            "temperature",
            "initial_prompt",
            "word_timestamps",
            "beam_size",
            "best_of",
            "patience",
            "condition_on_previous_text",
            "vad_filter",
        }
    )

    def __init__(self, endpoint: str, model_key: str):
        self.endpoint = endpoint.rstrip("/")
        self.model_key = model_key

    async def transcribe(
        self,
        audio_path: str,
        params: dict[str, Any] | None = None,
    ) -> SubtitleResult:
        merged_params: dict[str, Any] = dict(params or {})
        subtitle_format = str(merged_params.get("format", "srt"))
        language = str(merged_params.get("language", "auto"))

        form_fields = {
            "format": subtitle_format,
            "language": language,
        }
        allowed_passthrough_keys = self._ALLOWED_FORM_KEYS | {"output_path", "format", "language"}
        filtered_keys = [key for key in merged_params if key not in allowed_passthrough_keys]
        if filtered_keys:
            logger.warning("Filtered unsupported Whisper params: %s", sorted(filtered_keys))
        for key in self._ALLOWED_FORM_KEYS:
            if key in merged_params:
                form_fields[key] = merged_params[key]

        source_path = Path(audio_path)
        url = f"{self.endpoint}/transcribe"
        try:
            with source_path.open("rb") as audio_file:
                files = {
                    "file": (source_path.name, audio_file, "audio/wav"),
                }
                async with httpx.AsyncClient(timeout=180.0) as client:
                    response = await client.post(url, files=files, data=form_fields)
                    response.raise_for_status()
        except httpx.HTTPError as exc:
            raise map_httpx_error(
                exc, f"Failed to connect to Whisper STT provider at {url}"
            ) from exc

        body = response.json()
        segments = body.get("segments", [])
        full_text = str(body.get("text", ""))

        output_path = merged_params.get("output_path")
        if output_path:
            candidate_output = str(output_path)
            artifact_root = os.getenv("ARTIFACT_ROOT")
            validated_output = import_module("creator_domain.sanitize").validate_artifact_path(
                candidate_output,
                artifact_root or "data/artifacts",
            )
            destination = Path(validated_output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if subtitle_format == "vtt":
                destination.write_text(_segments_to_vtt(segments), encoding="utf-8")
            else:
                destination.write_text(_segments_to_srt(segments), encoding="utf-8")

        return SubtitleResult(
            segments=segments,
            full_text=full_text,
            model_key=self.model_key,
        )


def _format_srt_timestamp(seconds: float) -> str:
    clamped = max(0.0, float(seconds))
    hours = int(clamped // 3600)
    minutes = int((clamped % 3600) // 60)
    secs = int(clamped % 60)
    millis = int(round((clamped - int(clamped)) * 1000))
    if millis == 1000:
        millis = 0
        secs += 1
        if secs == 60:
            secs = 0
            minutes += 1
            if minutes == 60:
                minutes = 0
                hours += 1
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _segments_to_srt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for idx, segment in enumerate(segments, start=1):
        start = _format_srt_timestamp(float(segment.get("start", 0.0)))
        end = _format_srt_timestamp(float(segment.get("end", 0.0)))
        text = str(segment.get("text", "")).strip()
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _format_vtt_timestamp(seconds: float) -> str:
    clamped = max(0.0, float(seconds))
    hours = int(clamped // 3600)
    minutes = int((clamped % 3600) // 60)
    secs = int(clamped % 60)
    millis = int(round((clamped - int(clamped)) * 1000))
    if millis == 1000:
        millis = 0
        secs += 1
        if secs == 60:
            secs = 0
            minutes += 1
            if minutes == 60:
                minutes = 0
                hours += 1
    return f"{hours:02}:{minutes:02}:{secs:02}.{millis:03}"


def _segments_to_vtt(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = ["WEBVTT", ""]
    for idx, segment in enumerate(segments, start=1):
        start = _format_vtt_timestamp(float(segment.get("start", 0.0)))
        end = _format_vtt_timestamp(float(segment.get("end", 0.0)))
        text = str(segment.get("text", "")).strip()
        lines.append(str(idx))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)
