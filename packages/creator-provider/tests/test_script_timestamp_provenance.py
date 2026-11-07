from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from creator_provider.exceptions import ProviderError
from creator_provider.stt.script_timestamp_aligner import ScriptTimestampAligner


def _write_envelope(audio_path: Path, script: str) -> Path:
    sidecar = Path(f"{audio_path}.words.jsonl")
    sidecar.write_text(
        json.dumps(
            {
                "format": "short-form-studio/tts-word-timings",
                "version": 1,
                "audio_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                "words": [
                    {
                        "type": "WordBoundary",
                        "offset": 0,
                        "duration": 10_000_000,
                        "text": "same",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return sidecar


@pytest.mark.asyncio
async def test_alignment_rejects_sidecar_after_audio_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given metadata cryptographically paired with the original audio.
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"ORIGINAL_AUDIO_BYTES")
    _write_envelope(audio_path, "same")
    audio_path.write_bytes(b"NEW_AUDIO_BYTES")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    # When only the audio is replaced, then stale timing metadata fails closed.
    with pytest.raises(ProviderError, match="audio digest"):
        await ScriptTimestampAligner().transcribe(
            str(audio_path),
            params={"script_text": "same", "audio_duration_seconds": 1.0},
        )


@pytest.mark.asyncio
async def test_alignment_rejects_sidecar_after_exact_script_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given metadata paired with an exact TTS input containing punctuation.
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    _write_envelope(audio_path, "same!")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    # When normalized words match but exact script bytes differ, provenance fails.
    with pytest.raises(ProviderError, match="script digest"):
        await ScriptTimestampAligner().transcribe(
            str(audio_path),
            params={"script_text": "same", "audio_duration_seconds": 1.0},
        )


@pytest.mark.asyncio
async def test_alignment_rejects_unbound_replacement_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a legacy/replacement JSONL sidecar with matching words but no envelope.
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    Path(f"{audio_path}.words.jsonl").write_text(
        '{"type":"WordBoundary","offset":0,"duration":10000000,"text":"same"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    # When alignment runs, then unbound metadata cannot be substituted.
    with pytest.raises(ProviderError, match="Invalid TTS word timing metadata"):
        await ScriptTimestampAligner().transcribe(
            str(audio_path),
            params={"script_text": "same", "audio_duration_seconds": 1.0},
        )


@pytest.mark.asyncio
async def test_alignment_rejects_caller_controlled_sidecar_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given valid audio in the artifact root and matching metadata outside it.
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    audio_path = artifact_root / "audio.mp3"
    audio_path.write_bytes(b"audio")
    outside = tmp_path / "outside.words.jsonl"
    expected = _write_envelope(audio_path, "same")
    expected.replace(outside)
    monkeypatch.setenv("ARTIFACT_ROOT", str(artifact_root))

    # When a caller supplies the outside path, then the override is rejected.
    with pytest.raises(ProviderError, match="caller-controlled"):
        await ScriptTimestampAligner().transcribe(
            str(audio_path),
            params={
                "script_text": "same",
                "audio_duration_seconds": 1.0,
                "word_timestamps_path": str(outside),
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", ["not-a-number", object()])
async def test_alignment_maps_malformed_duration_to_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    duration: object,
) -> None:
    # Given valid paired metadata and a malformed duration value.
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    _write_envelope(audio_path, "same")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

    # When duration parsing fails, then the provider's typed error contract holds.
    with pytest.raises(
        ProviderError,
        match="Script alignment requires a positive finite audio duration",
    ):
        await ScriptTimestampAligner().transcribe(
            str(audio_path),
            params={"script_text": "same", "audio_duration_seconds": duration},
        )
