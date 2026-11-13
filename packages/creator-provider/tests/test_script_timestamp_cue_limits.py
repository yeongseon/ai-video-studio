"""Cue splitting at readability limits, through the public provider boundary."""

from pathlib import Path
from math import ceil

import pytest

from creator_provider.stt.script_timestamp_aligner import ScriptTimestampAligner
from creator_provider.word_timing_envelope import WordTimingRow, envelope, write_atomic


@pytest.mark.asyncio
@pytest.mark.parametrize("repetitions", [1, 3])
async def test_korean_paragraph_splits_before_duration_overflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repetitions: int,
) -> None:
    # Given a 10- or 30-eojeol paragraph with 0.45s words separated by 0.05s.
    words = (
        "오늘은 정말 좋은 날에 작은 이야기를 함께 나누니 끝까지 들어주세요".split() * repetitions
    )
    script = " ".join(words) + "!"
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    rows: list[WordTimingRow] = [
        {"type": "WordBoundary", "offset": index * 5_000_000, "duration": 4_500_000, "text": word}
        for index, word in enumerate(words)
    ]
    write_atomic(Path(f"{audio}.words.jsonl"), envelope(audio, script, rows))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # When the paragraph crosses multiple four-second cue windows.
    result = await ScriptTimestampAligner().transcribe(
        str(audio),
        params={
            "script_text": script,
            "audio_duration_seconds": len(words) * 0.5,
        },
    )
    # Then every word and punctuation survives exactly once, in readable ordered cues.
    assert len(result.segments) >= ceil(len(words) * 0.5 / 4.0)
    assert " ".join(segment["text"] for segment in result.segments) == script
    assert all(len(segment["text"]) <= 42 for segment in result.segments)
    assert all(0 < segment["end"] - segment["start"] <= 4.0 for segment in result.segments)
    assert all(
        current["end"] <= following["start"]
        for current, following in zip(result.segments, result.segments[1:])
    )
    assert result.segments[-1]["end"] <= len(words) * 0.5


@pytest.mark.asyncio
async def test_trailing_silence_does_not_make_final_cue_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given three spoken words ending at 3.2s, with trailing silence until 4.3s.
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    script = "하나 둘 셋"
    rows: list[WordTimingRow] = [
        {"type": "WordBoundary", "offset": 0, "duration": 9_000_000, "text": "하나"},
        {"type": "WordBoundary", "offset": 10_000_000, "duration": 10_000_000, "text": "둘"},
        {"type": "WordBoundary", "offset": 21_000_000, "duration": 11_000_000, "text": "셋"},
    ]
    write_atomic(Path(f"{audio}.words.jsonl"), envelope(audio, script, rows))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # When the last cue is extended toward the measured audio end.
    result = await ScriptTimestampAligner().transcribe(
        str(audio),
        params={
            "script_text": script,
            "audio_duration_seconds": 4.3,
        },
    )
    # Then trailing silence is capped, not a fatal readability error.
    assert result.segments == [{"start": 0.0, "end": 4.0, "text": script}]
