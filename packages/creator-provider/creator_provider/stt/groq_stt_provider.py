from __future__ import annotations

import logging
import os
from importlib import import_module
from pathlib import Path
from typing import Any

import httpx

from creator_provider.api_keys import resolve_api_key
from creator_provider.base import STTProvider, SubtitleResult
from creator_provider.exceptions import ProviderError, map_httpx_error


logger = logging.getLogger(__name__)


class GroqSTTProvider(STTProvider):
    """Speech-to-text provider using Groq's Whisper API (OpenAI-compatible)."""

    def __init__(self, endpoint: str, model_key: str):
        self.endpoint = endpoint.rstrip("/")
        self.model_key = model_key
        self.api_key: str = resolve_api_key("groq")

    async def transcribe(
        self,
        audio_path: str,
        params: dict[str, Any] | None = None,
    ) -> SubtitleResult:
        merged_params: dict[str, Any] = dict(params or {})
        language = merged_params.get("language", "ko")
        response_format = "verbose_json"

        source_path = Path(audio_path)
        url = f"{self.endpoint}/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        form_data = {
            "model": self.model_key.removeprefix("groq-"),
            "language": language,
            "response_format": response_format,
            "timestamp_granularities[]": "segment",
        }

        import asyncio

        max_retries = 5
        last_exc: Exception | None = None

        for attempt in range(max_retries):
            try:
                with source_path.open("rb") as audio_file:
                    files = {"file": (source_path.name, audio_file, "audio/wav" if source_path.suffix == ".wav" else "audio/mpeg")}
                    async with httpx.AsyncClient(timeout=180.0) as client:
                        response = await client.post(url, headers=headers, data=form_data, files=files)
                        if response.status_code == 429:
                            wait_time = min(10 * (2 ** attempt), 60)
                            logger.warning(
                                "Groq STT rate limited, waiting %ds (attempt %d/%d)",
                                wait_time, attempt + 1, max_retries,
                            )
                            await asyncio.sleep(wait_time)
                            continue
                        response.raise_for_status()
            except httpx.HTTPError as exc:
                last_exc = exc
                err_msg = str(exc).lower()
                if "429" in err_msg or "rate limit" in err_msg or "too many requests" in err_msg:
                    wait_time = min(10 * (2 ** attempt), 60)
                    logger.warning(
                        "Groq STT rate limited (exception), waiting %ds (attempt %d/%d)",
                        wait_time, attempt + 1, max_retries,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                raise map_httpx_error(exc, "Groq Whisper API") from exc
            break
        else:
            if last_exc:
                raise map_httpx_error(last_exc, "Groq Whisper API") from last_exc
            raise ProviderError("Groq STT rate limited after max retries")

        body = response.json()
        full_text = str(body.get("text", ""))
        segments = body.get("segments", [])

        # Normalize segment format to match local Whisper output
        normalized_segments = []
        for seg in segments:
            normalized_segments.append(
                {
                    "start": float(seg.get("start", 0.0)),
                    "end": float(seg.get("end", 0.0)),
                    "text": str(seg.get("text", "")).strip(),
                }
            )

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
            subtitle_format = merged_params.get("format") or merged_params.get("subtitle_format", "srt")
            if subtitle_format == "vtt":
                destination.write_text(_segments_to_vtt(normalized_segments), encoding="utf-8")
            else:
                destination.write_text(_segments_to_srt(normalized_segments), encoding="utf-8")

        return SubtitleResult(
            segments=normalized_segments,
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
