from __future__ import annotations

import os
import tempfile
from importlib import import_module
from pathlib import Path
from typing import Any

import edge_tts
from billiard.exceptions import SoftTimeLimitExceeded

from creator_provider.base import AudioResult, TTSProvider
from creator_provider.exceptions import ProviderError
from creator_provider.validation import MAX_TTS_TEXT_CHARS, validate_prompt_length
from creator_provider.word_timing_envelope import envelope, parse_sdk_rows, write_atomic

# Default Korean voices
_DEFAULT_VOICES: dict[str, str] = {
    "default": "ko-KR-SunHiNeural",
    "female": "ko-KR-SunHiNeural",
    "male": "ko-KR-InJoonNeural",
    "en-female": "en-US-JennyNeural",
    "en-male": "en-US-GuyNeural",
}


class EdgeTTSProvider(TTSProvider):
    """Free TTS via Microsoft Edge TTS — no API key required, unlimited usage."""

    def __init__(self, endpoint: str, model_key: str):
        # endpoint is unused for edge-tts (local library), but kept for interface compatibility
        self.endpoint: str = endpoint
        self.model_key: str = model_key

    async def generate(
        self,
        text: str,
        voice: str = "default",
        params: dict[str, Any] | None = None,
    ) -> AudioResult:
        validate_prompt_length(text, MAX_TTS_TEXT_CHARS, "TTS")
        merged_params = dict(params or {})

        # Resolve voice name
        voice_name = _DEFAULT_VOICES.get(voice, voice)
        if voice != "default" and voice not in _DEFAULT_VOICES:
            # Assume it's a direct Edge TTS voice name (e.g. "ko-KR-SunHiNeural")
            voice_name = voice

        # Determine output path
        output_path_str = merged_params.get("output_path")
        if output_path_str:
            candidate_output = str(output_path_str)
            artifact_root = os.getenv("ARTIFACT_ROOT")
            validated_output = import_module("creator_domain.sanitize").validate_artifact_path(
                candidate_output,
                artifact_root or "data/artifacts",
            )
            output_path = Path(validated_output)
        else:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                output_path = Path(tmp.name)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Support rate/pitch/volume parameters for quality control
            rate = merged_params.get("rate", "+0%")  # e.g. "+10%", "-5%"
            pitch = merged_params.get("pitch", "+0Hz")  # e.g. "+5Hz", "-2Hz"
            volume = merged_params.get("volume", "+0%")  # e.g. "+10%", "-20%"
            communicate = edge_tts.Communicate(
                text=text, voice=voice_name, rate=rate, pitch=pitch, volume=volume
            )
            word_timestamps_path = Path(f"{output_path}.words.jsonl")
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent, suffix=".words.jsonl", delete=False
            ) as metadata_file:
                sdk_metadata_path = Path(metadata_file.name)
            try:
                await communicate.save(str(output_path), str(sdk_metadata_path))
                words = parse_sdk_rows(sdk_metadata_path.read_text(encoding="utf-8"))
                write_atomic(word_timestamps_path, envelope(output_path, text, words))
            finally:
                sdk_metadata_path.unlink(missing_ok=True)
        except SoftTimeLimitExceeded:
            raise
        except ConnectionError as exc:
            from creator_provider.exceptions import ProviderTimeoutError
            raise ProviderTimeoutError(f"Edge TTS connection failed: {exc}") from exc
        except Exception as exc:
            raise ProviderError(f"Edge TTS generation failed: {exc}") from exc

        # Estimate duration from file size (MP3 ~128kbps)
        file_size = output_path.stat().st_size
        if file_size < 100:
            raise ProviderError("Edge TTS produced empty or invalid audio")
        duration = float(file_size) / (128 * 1000 / 8)

        return AudioResult(
            audio_path=str(output_path),
            duration_seconds=duration,
            model_key=self.model_key,
            metadata={"word_timestamps_path": str(word_timestamps_path)},
        )
