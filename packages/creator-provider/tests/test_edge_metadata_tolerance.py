"""Offline SDK contract and optional metadata failure coverage."""

import inspect
import json
from pathlib import Path
from typing import get_args

import edge_tts
import pytest

from creator_provider.tts.edge_tts_provider import EdgeTTSProvider


def test_real_sdk_accepts_word_boundary_parameter() -> None:
    # Given the installed, pinned SDK (not a fake).
    signature = inspect.signature(edge_tts.Communicate.__init__)
    # When inspecting its supported boundary values.
    boundary = signature.parameters["boundary"]
    # Then the adapter's required value is supported without any network call.
    assert "WordBoundary" in get_args(boundary.annotation)
    signature.bind(None, text="offline", boundary="WordBoundary")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        None,
        "not-json",
        '{"type":"SentenceBoundary","text":"hello"}',
        '{"type":"WordBoundary","text":"hello"}',
    ],
)
async def test_audio_survives_unusable_sdk_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given successful audio bytes but missing or malformed SDK metadata and a stale envelope.
    output = tmp_path / "audio.mp3"
    sidecar = Path(f"{output}.words.jsonl")
    sidecar.write_text("stale", encoding="utf-8")

    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            self.text = kwargs["text"]

        async def save(self, path: str, metadata_path: str | None = None) -> None:
            Path(path).write_bytes(b"audio" * 100)
            assert metadata_path is not None
            if metadata is None:
                Path(metadata_path).unlink()
            else:
                Path(metadata_path).write_text(metadata, encoding="utf-8")

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # When generating audio, metadata is not a prerequisite.
    result = await EdgeTTSProvider("", "edge-tts").generate(
        "hello",
        params={"output_path": str(output)},
    )
    # Then audio is returned, unusable metadata is invalidated and operators are warned.
    assert Path(result.audio_path).read_bytes() == b"audio" * 100
    assert result.metadata == {}
    assert not sidecar.exists()
    assert list(tmp_path.glob("*.words.jsonl")) == []
    assert any(record.levelname == "WARNING" for record in caplog.records)


@pytest.mark.asyncio
async def test_temporary_audio_does_not_publish_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given generation without a persistent artifact output path.
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))

    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            self.text = kwargs["text"]

        async def save(self, path: str, metadata_path: str | None = None) -> None:
            Path(path).write_bytes(b"audio" * 100)
            if metadata_path is not None:
                Path(metadata_path).write_text(
                    json.dumps(
                        {
                            "type": "WordBoundary",
                            "offset": 0,
                            "duration": 1_000_000,
                            "text": self.text,
                        }
                    ),
                    encoding="utf-8",
                )

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    # When a caller asks only for temporary audio.
    result = await EdgeTTSProvider("", "edge-tts").generate("hello")
    # Then there is no leaked timing envelope or SDK metadata.
    assert Path(result.audio_path).read_bytes() == b"audio" * 100
    assert result.metadata == {}
    assert list(tmp_path.glob("*.words.jsonl")) == []


@pytest.mark.asyncio
async def test_audio_survives_metadata_staging_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an artifact destination but no usable metadata staging file.
    output = tmp_path / "audio.mp3"

    class FakeCommunicate:
        def __init__(self, **kwargs: str) -> None:
            self.text = kwargs["text"]

        async def save(self, path: str, metadata_path: str | None = None) -> None:
            assert metadata_path is None
            Path(path).write_bytes(b"audio" * 100)

    def failed_staging(**kwargs: str | bool) -> None:
        raise OSError("metadata staging unavailable")

    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate
    )
    monkeypatch.setattr(
        "creator_provider.tts.edge_tts_provider.tempfile.NamedTemporaryFile", failed_staging
    )
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    # When metadata cannot even be staged, audio generation still proceeds once.
    result = await EdgeTTSProvider("", "edge-tts").generate(
        "hello",
        params={"output_path": str(output)},
    )
    # Then only usable audio is returned.
    assert Path(result.audio_path).read_bytes() == b"audio" * 100
    assert result.metadata == {}
