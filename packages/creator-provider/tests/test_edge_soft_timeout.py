import hashlib
import json
from pathlib import Path

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from creator_provider.exceptions import ProviderError
from creator_provider.tts.edge_tts_provider import EdgeTTSProvider


@pytest.mark.asyncio
async def test_edge_sdk_soft_timeout_is_not_wrapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an Edge SDK save interrupted by the worker soft deadline.
    calls: list[tuple[str, str | None]] = []

    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            assert kwargs["voice"] == "ko-KR-SunHiNeural"

        async def save(self, path: str, metadata_path: str | None = None) -> None:
            calls.append((path, metadata_path))
            raise SoftTimeLimitExceeded()

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    output = tmp_path / "timeout.mp3"
    # When the adapter awaits the real SDK boundary.
    with pytest.raises(SoftTimeLimitExceeded):
        await EdgeTTSProvider(endpoint="", model_key="edge-tts").generate(
            "hello",
            params={"output_path": str(output)},
        )
    # Then the caller receives the timeout identity and only one save was attempted.
    assert len(calls) == 1
    assert calls[0][0] == str(output)
    assert calls[0][1] is not None
    assert calls[0][1] != f"{output}.words.jsonl"
    assert not output.exists()
    assert not Path(f"{output}.words.jsonl").exists()


@pytest.mark.asyncio
async def test_edge_other_sdk_failures_remain_provider_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an unrelated SDK failure.
    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            pass

        async def save(self, path: str, _metadata_path: str | None = None) -> None:
            raise RuntimeError("SDK failed")

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # When generation fails.
    with pytest.raises(ProviderError):
        await EdgeTTSProvider(endpoint="", model_key="edge-tts").generate(
            "hello",
            params={"output_path": str(tmp_path / "other.mp3")},
        )


@pytest.mark.asyncio
async def test_edge_tts_saves_word_boundaries_next_to_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given Edge TTS generation for an artifact path.
    saved_paths: list[tuple[str, str | None]] = []

    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            assert kwargs["text"] == "정확한 대본"

        async def save(self, audio_path: str, metadata_path: str | None = None) -> None:
            saved_paths.append((audio_path, metadata_path))
            Path(audio_path).write_bytes(b"x" * 101)
            assert metadata_path is not None
            Path(metadata_path).write_text(
                '{"type":"WordBoundary","offset":0,"duration":10000000,"text":"정확한"}\n',
                encoding="utf-8",
            )

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    output = tmp_path / "audio.mp3"

    # When audio is generated.
    result = await EdgeTTSProvider(endpoint="", model_key="edge-tts").generate(
        "정확한 대본",
        params={"output_path": str(output)},
    )

    # Then provider-native word timings are retained beside that exact audio.
    metadata_path = tmp_path / "audio.mp3.words.jsonl"
    assert saved_paths[0][0] == str(output)
    assert saved_paths[0][1] is not None
    assert saved_paths[0][1] != str(metadata_path)
    assert metadata_path.exists()
    envelope = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert envelope == {
        "format": "short-form-studio/tts-word-timings",
        "version": 2,
        "audio_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256("정확한 대본".encode()).hexdigest(),
        "script_text": "정확한 대본",
        "words": [
            {
                "type": "WordBoundary",
                "offset": 0,
                "duration": 10_000_000,
                "text": "정확한",
            }
        ],
    }
    assert result.metadata == {"word_timestamps_path": str(metadata_path)}
