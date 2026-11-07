from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from creator_provider.exceptions import ProviderError
from creator_provider.stt.script_timestamp_aligner import ScriptTimestampAligner


@pytest.fixture(autouse=True)
def _artifact_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))


def _write_boundaries(
    audio_path: Path,
    rows: list[dict[str, int | str]],
    script: str | None = None,
) -> Path:
    metadata_path = audio_path.with_suffix(f"{audio_path.suffix}.words.jsonl")
    metadata_path.write_text(
        json.dumps(
            {
                "format": "short-form-studio/tts-word-timings",
                "version": 1,
                "audio_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(
                    (script or " ".join(str(row["text"]) for row in rows)).encode()
                ).hexdigest(),
                "words": rows,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return metadata_path


@pytest.mark.asyncio
async def test_alignment_preserves_known_script_and_readable_cues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given provider word boundaries for two approved-script paragraphs.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    script = "2026년, state-of-the-art 제품입니다.\n안녕하세요—정확한 자막이에요!"
    _write_boundaries(
        audio_path,
        [
            {"type": "WordBoundary", "offset": 0, "duration": 8_000_000, "text": "2026년"},
            {
                "type": "WordBoundary",
                "offset": 9_000_000,
                "duration": 10_000_000,
                "text": "state-of-the-art",
            },
            {
                "type": "WordBoundary",
                "offset": 20_000_000,
                "duration": 8_000_000,
                "text": "제품입니다",
            },
            {
                "type": "WordBoundary",
                "offset": 30_000_000,
                "duration": 9_000_000,
                "text": "안녕하세요",
            },
            {
                "type": "WordBoundary",
                "offset": 40_000_000,
                "duration": 8_000_000,
                "text": "정확한",
            },
            {
                "type": "WordBoundary",
                "offset": 49_000_000,
                "duration": 8_000_000,
                "text": "자막이에요",
            },
        ],
        script,
    )
    output_path = tmp_path / "captions.srt"
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    aligner = ScriptTimestampAligner(model_key="script-timestamps")

    # When the known script is aligned to the TTS provider timestamps.
    result = await aligner.transcribe(
        str(audio_path),
        params={
            "script_text": script,
            "audio_duration_seconds": 6.0,
            "format": "srt",
            "output_path": str(output_path),
        },
    )

    # Then text is script-ground-truth, paragraph boundaries remain separate,
    # and cues are readable, monotonic, non-overlapping, and audio-bounded.
    assert " ".join(segment["text"] for segment in result.segments) == script.replace("\n", " ")
    assert result.segments[0]["text"] == "2026년, state-of-the-art 제품입니다."
    assert result.segments[1]["text"] == "안녕하세요—정확한 자막이에요!"
    assert all(len(segment["text"]) <= 42 for segment in result.segments)
    assert all(segment["end"] - segment["start"] <= 4.0 for segment in result.segments)
    assert all(
        current["end"] <= following["start"]
        for current, following in zip(result.segments, result.segments[1:])
    )
    assert result.segments[-1]["end"] == 6.0
    assert output_path.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:02,800\n2026년, state-of-the-art 제품입니다.\n\n"
        "2\n00:00:03,000 --> 00:00:06,000\n안녕하세요—정확한 자막이에요!\n"
    )


@pytest.mark.asyncio
async def test_alignment_writes_valid_vtt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given exact provider timings for the approved script.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    _write_boundaries(
        audio_path,
        [{"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "Hello"}],
        "Hello!",
    )
    output_path = tmp_path / "captions.vtt"
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    # When VTT is requested.
    await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
        str(audio_path),
        params={
            "script_text": "Hello!",
            "audio_duration_seconds": 1.25,
            "format": "vtt",
            "output_path": str(output_path),
        },
    )

    # Then the output has a valid WebVTT header and timestamp syntax.
    assert output_path.read_text(encoding="utf-8") == (
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:01.250\nHello!\n"
    )


@pytest.mark.asyncio
async def test_alignment_fails_safely_without_tts_metadata(tmp_path: Path) -> None:
    # Given audio from a provider that did not emit timing metadata.
    audio_path = tmp_path / "narration.wav"
    audio_path.write_bytes(b"audio")

    # When script alignment is explicitly selected, then no timing is invented.
    with pytest.raises(ProviderError, match="word timing metadata"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path), params={"script_text": "Known script"}
        )


@pytest.mark.asyncio
async def test_alignment_rejects_mismatched_script_and_audio(tmp_path: Path) -> None:
    # Given provider metadata whose spoken words do not match the approved script.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    _write_boundaries(
        audio_path,
        [{"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "different"}],
    )

    # When alignment runs, then it reports the mismatch rather than substituting text.
    with pytest.raises(ProviderError, match="does not match"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path),
            params={"script_text": "Known script", "audio_duration_seconds": 1.0},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows,duration",
    [
        (
            [
                {"type": "WordBoundary", "offset": 10_000_000, "duration": 10_000_000, "text": "one"},
                {"type": "WordBoundary", "offset": 5_000_000, "duration": 10_000_000, "text": "two"},
            ],
            2.0,
        ),
        ([{"type": "WordBoundary", "offset": -1, "duration": 1, "text": "one"}], 1.0),
        ([{"type": "WordBoundary", "offset": 0, "duration": 20_000_000, "text": "one"}], 1.0),
    ],
)
async def test_alignment_rejects_invalid_timing_boundaries(
    tmp_path: Path, rows: list[dict[str, int | str]], duration: float
) -> None:
    # Given malformed, overlapping, or audio-out-of-bounds provider timings.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    _write_boundaries(audio_path, rows)

    # When alignment runs, then invalid timing metadata fails closed.
    with pytest.raises(ProviderError, match="timing"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path),
            params={"script_text": "one two" if len(rows) == 2 else "one", "audio_duration_seconds": duration},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [None, 0.0, float("inf")])
async def test_alignment_requires_measured_finite_audio_duration(
    tmp_path: Path, duration: float | None
) -> None:
    # Given valid word timing metadata but absent or invalid measured duration.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    _write_boundaries(
        audio_path,
        [{"type": "WordBoundary", "offset": 0, "duration": 10_000_000, "text": "one"}],
    )
    params: dict[str, object] = {"script_text": "one"}
    if duration is not None:
        params["audio_duration_seconds"] = duration

    # When alignment runs, then an actual valid audio bound is mandatory.
    with pytest.raises(ProviderError, match="audio duration"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path), params=params
        )


@pytest.mark.asyncio
async def test_alignment_rejects_unreadable_single_word_cue(tmp_path: Path) -> None:
    # Given one provider boundary that cannot satisfy readable cue limits.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    long_word = "x" * 43
    _write_boundaries(
        audio_path,
        [{"type": "WordBoundary", "offset": 0, "duration": 50_000_000, "text": long_word}],
    )

    # When alignment runs, then it refuses to emit an overlong cue.
    with pytest.raises(ProviderError, match="readable cue limits"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path), params={"script_text": long_word, "audio_duration_seconds": 5.0}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    ["not-json\n", "[]\n", '{"type":"WordBoundary","text":"one"}\n'],
)
async def test_alignment_maps_malformed_metadata_to_provider_error(
    tmp_path: Path, metadata: str
) -> None:
    # Given malformed or structurally invalid provider metadata.
    audio_path = tmp_path / "narration.mp3"
    audio_path.write_bytes(b"audio")
    Path(f"{audio_path}.words.jsonl").write_text(metadata, encoding="utf-8")

    # When alignment runs, then the provider boundary fails consistently.
    with pytest.raises(ProviderError, match="Invalid TTS word timing metadata"):
        await ScriptTimestampAligner(model_key="script-timestamps").transcribe(
            str(audio_path), params={"script_text": "one", "audio_duration_seconds": 1.0}
        )
