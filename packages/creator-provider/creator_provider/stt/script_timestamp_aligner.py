from __future__ import annotations

import json
import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict, assert_never, override

from creator_domain.provider_errors import ProviderError
from creator_domain.sanitize import validate_artifact_path
from creator_provider.base import STTProvider, SubtitleResult
from creator_provider.word_timing_envelope import FORMAT, VERSION

_WORD_PATTERN: Final = re.compile(r"[\w]+(?:-[\w]+)*", re.UNICODE)
_MAX_CUE_CHARS: Final = 42
_MAX_CUE_SECONDS: Final = 4.0
_TICKS_PER_SECOND: Final = 10_000_000


@dataclass(frozen=True, slots=True)
class _TimedWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class _ScriptWord:
    text: str
    start: int
    end: int
    paragraph: int


class _SubtitleSegment(TypedDict):
    start: float
    end: float
    text: str


class ScriptTimestampAligner(STTProvider):
    """Align approved script text to provider-native TTS word boundaries."""

    def __init__(self, endpoint: str = "", model_key: str = "script-timestamps") -> None:
        self.endpoint: str = endpoint
        self.model_key: str = model_key

    @override
    async def transcribe(
        self,
        audio_path: str,
        params: dict[str, object] | None = None,
    ) -> SubtitleResult:
        merged_params = dict(params or {})
        script_text = str(merged_params.get("script_text", "")).strip()
        if "word_timestamps_path" in merged_params:
            raise ProviderError("Script alignment rejects caller-controlled timing metadata paths")
        artifact_root = os.getenv("ARTIFACT_ROOT") or "data/artifacts"
        validated_audio = Path(validate_artifact_path(audio_path, artifact_root))
        metadata_path = Path(
            validate_artifact_path(f"{validated_audio}.words.jsonl", artifact_root)
        )
        if not metadata_path.is_file():
            raise ProviderError(
                f"Script alignment requires TTS word timing metadata: {metadata_path}"
            )

        timed_words = self._read_timed_words(metadata_path, validated_audio, script_text)
        script_words = self._script_words(script_text)
        if [self._normalized(word.text) for word in timed_words] != [
            self._normalized(word.text) for word in script_words
        ]:
            raise ProviderError("Approved script does not match TTS word timing metadata")

        duration_value = merged_params.get("audio_duration_seconds")
        if duration_value is None:
            raise ProviderError("Script alignment requires a measured audio duration")
        try:
            audio_duration = self._as_float(duration_value)
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "Script alignment requires a positive finite audio duration"
            ) from exc
        if not math.isfinite(audio_duration) or audio_duration <= 0:
            raise ProviderError("Script alignment requires a positive finite audio duration")
        self._validate_timing(timed_words, audio_duration)
        segments = self._segments(script_text, script_words, timed_words, audio_duration)
        subtitle_format = str(merged_params.get("format", "srt"))
        output_path = merged_params.get("output_path")
        if output_path is not None:
            artifact_root = os.getenv("ARTIFACT_ROOT")
            validated = validate_artifact_path(str(output_path), artifact_root or "data/artifacts")
            destination = Path(validated)
            destination.parent.mkdir(parents=True, exist_ok=True)
            rendered = self._to_vtt(segments) if subtitle_format == "vtt" else self._to_srt(segments)
            _ = destination.write_text(rendered, encoding="utf-8")

        return SubtitleResult(
            segments=segments,
            full_text=script_text,
            model_key=self.model_key,
            metadata={"alignment_source": "tts_word_boundaries"},
        )

    @staticmethod
    def _read_timed_words(path: Path, audio_path: Path, script_text: str) -> list[_TimedWord]:
        words: list[_TimedWord] = []
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise TypeError
            if document["format"] != FORMAT or document["version"] != VERSION:
                raise ValueError
            audio_digest = hashlib.sha256(audio_path.read_bytes()).hexdigest()
            if document["audio_sha256"] != audio_digest:
                raise ProviderError("TTS word timing audio digest does not match audio")
            script_digest = hashlib.sha256(script_text.encode()).hexdigest()
            if document["script_sha256"] != script_digest:
                raise ProviderError("TTS word timing script digest does not match script")
            rows = document["words"]
            if not isinstance(rows, list):
                raise TypeError
            for row in rows:
                if not isinstance(row, dict) or row.get("type") != "WordBoundary":
                    raise TypeError
                offset = int(row["offset"])
                duration = int(row["duration"])
                words.append(
                    _TimedWord(
                        text=str(row["text"]),
                        start=offset / _TICKS_PER_SECOND,
                        end=(offset + duration) / _TICKS_PER_SECOND,
                    )
                )
        except ProviderError:
            raise
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"Invalid TTS word timing metadata: {path}") from exc
        if not words:
            raise ProviderError(f"No TTS word timing metadata found in {path}")
        return words

    @staticmethod
    def _as_float(value: object) -> float:
        match value:
            case int() | float() | str():
                return float(value)
            case _:
                raise TypeError

    @staticmethod
    def _validate_timing(words: list[_TimedWord], audio_duration: float) -> None:
        previous_end = 0.0
        for word in words:
            if word.start < previous_end or word.end <= word.start or word.end > audio_duration:
                raise ProviderError("TTS word timing metadata is invalid or outside the audio duration")
            if word.end - word.start > _MAX_CUE_SECONDS:
                raise ProviderError("TTS word timing cannot satisfy readable cue limits")
            previous_end = word.end

    @staticmethod
    def _script_words(script: str) -> list[_ScriptWord]:
        words: list[_ScriptWord] = []
        paragraph = 0
        cursor = 0
        for line in script.splitlines(keepends=True):
            for match in _WORD_PATTERN.finditer(line):
                words.append(
                    _ScriptWord(
                        text=match.group(),
                        start=cursor + match.start(),
                        end=cursor + match.end(),
                        paragraph=paragraph,
                    )
                )
            cursor += len(line)
            paragraph += 1
        if not words:
            raise ProviderError("Approved script has no alignable words")
        return words

    @staticmethod
    def _normalized(text: str) -> str:
        return "".join(character for character in text.casefold() if character.isalnum())

    @classmethod
    def _segments(
        cls,
        script: str,
        script_words: list[_ScriptWord],
        timed_words: list[_TimedWord],
        audio_duration: float,
    ) -> list[_SubtitleSegment]:
        segments: list[_SubtitleSegment] = []
        cue_start = 0
        for index, (script_word, timed_word) in enumerate(zip(script_words, timed_words)):
            next_word = script_words[index + 1] if index + 1 < len(script_words) else None
            cue_text = script[script_words[cue_start].start : script_word.end].strip()
            paragraph_ends = next_word is None or next_word.paragraph != script_word.paragraph
            duration_limit = timed_word.end - timed_words[cue_start].start >= _MAX_CUE_SECONDS
            length_limit = next_word is not None and len(
                script[script_words[cue_start].start : next_word.end].strip()
            ) > _MAX_CUE_CHARS
            if paragraph_ends or duration_limit or length_limit:
                text_end = next_word.start if next_word is not None else len(script)
                text = script[script_words[cue_start].start:text_end].strip()
                end = min(timed_word.end, audio_duration)
                if next_word is None:
                    end = audio_duration
                segments.append(
                    {"start": timed_words[cue_start].start, "end": end, "text": text or cue_text}
                )
                if len(segments[-1]["text"]) > _MAX_CUE_CHARS or end - timed_words[cue_start].start > _MAX_CUE_SECONDS:
                    raise ProviderError("TTS word timing cannot satisfy readable cue limits")
                cue_start = index + 1
        return segments

    @staticmethod
    def _timestamp(seconds: float, separator: str) -> str:
        milliseconds = round(max(0.0, seconds) * 1000)
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"

    @classmethod
    def _to_srt(cls, segments: list[_SubtitleSegment]) -> str:
        blocks = [
            f"{index}\n{cls._timestamp(segment['start'], ',')} --> "
            + f"{cls._timestamp(segment['end'], ',')}\n{segment['text']}"
            for index, segment in enumerate(segments, start=1)
        ]
        return "\n\n".join(blocks) + "\n"

    @classmethod
    def _to_vtt(cls, segments: list[_SubtitleSegment]) -> str:
        blocks = [
            f"{index}\n{cls._timestamp(segment['start'], '.')} --> "
            + f"{cls._timestamp(segment['end'], '.')}\n{segment['text']}"
            for index, segment in enumerate(segments, start=1)
        ]
        return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
