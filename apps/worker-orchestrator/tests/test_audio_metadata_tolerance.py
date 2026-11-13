import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_domain.models.script_draft import ScriptSection
from creator_provider.exceptions import ProviderError
from creator_provider.stt.script_timestamp_aligner import ScriptTimestampAligner
from creator_provider.word_timing_envelope import WordTimingRow, envelope, write_atomic
from tasks import generate_audio as audio
from tasks import generate_subtitles as subtitles

from .test_generate_audio import (
    FakeAudioService,
    FakeEntry,
    FakeProvider,
    FakeRegistry,
    FakeScriptDraft,
    FakeScriptService,
    _make_storage,
    _patch_services,
)
from .test_generate_subtitles import (
    FakeAudioArtifact,
    FakeAudioService as SubtitleAudioService,
    FakeEntry as SubtitleEntry,
    FakeProvider as SubtitleProvider,
    FakeRegistry as SubtitleRegistry,
    FakeSubtitleService,
)


@pytest.mark.parametrize(
    "metadata", ["missing", "malformed", "overlap", "digest", "empty", "probe-timeout", "soft-timeout"]
)
def test_default_audio_and_subtitles_survive_unusable_section_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given successful section audio with unusable metadata, and the default Whisper path.
    class SectionProvider(FakeProvider):
        async def generate(
            self, text: str, voice: str = "default", params: dict[str, object] | None = None
        ) -> None:
            await super().generate(text, voice, params)
            assert params is not None
            path = Path(str(params["output_path"]))
            path.write_bytes(b"audio" * 100)
            if metadata == "missing":
                return
            sidecar = Path(f"{path}.words.jsonl")
            if metadata == "malformed":
                sidecar.write_text("not-json", encoding="utf-8")
                return
            rows: list[WordTimingRow] = [
                {
                    "type": "WordBoundary",
                    "offset": 0,
                    "duration": 5_000_000,
                    "text": "" if metadata == "empty" else text,
                }
            ]
            if metadata == "overlap":
                rows.append({**rows[0], "offset": 1})
            document = envelope(path, text, rows)
            if metadata == "digest":
                document["audio_sha256"] = "wrong"
            write_atomic(sidecar, document)

    class SectionAudioService(FakeAudioService):
        async def create_paragraph_artifact(self, **kwargs: str | int) -> None:
            assert kwargs["run_id"] == 101

    def concatenate(paths: list[str], output: str) -> None:
        Path(output).write_bytes(b"".join(Path(path).read_bytes() for path in paths))
        Path(f"{output}.words.jsonl").write_text("stale", encoding="utf-8")

    provider = SectionProvider()
    draft = FakeScriptDraft(
        markdown_content="Opening. Ending.",
        structured_script=[
            ScriptSection(section_id="opening", type="hook", text="Opening."),
            ScriptSection(section_id="ending", type="conclusion", text="Ending."),
        ],
    )
    scripts = FakeScriptService(draft)
    storage = _make_storage()
    _patch_services(monkeypatch, scripts, SectionAudioService(), storage)
    monkeypatch.setattr(
        audio,
        "get_default_registry",
        lambda: FakeRegistry(
            FakeEntry(provider_type="edge_tts", requires_gpu=False),
            provider,
        ),
    )
    monkeypatch.setattr(audio, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(audio, "record_provider_call", AsyncMock())
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.concatenate_audio",
        lambda _self, paths, output: concatenate(paths, output),
    )
    timeout = SoftTimeLimitExceeded()

    def probe_duration(_self: object, _path: str) -> float:
        match metadata:
            case "probe-timeout":
                raise subprocess.TimeoutExpired("ffprobe", 30)
            case "soft-timeout":
                raise timeout
        return 1.0

    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.get_audio_duration", probe_duration
    )
    # When the audio task completes and default subtitle generation consumes its artifact.
    if metadata == "soft-timeout":
        with pytest.raises(SoftTimeLimitExceeded) as raised:
            audio.generate_audio.run(run_id=101, tts_model="edge-tts")
        assert raised.value is timeout
        assert [call[0] for call in provider.calls] == ["Opening.", "Ending."]
        return
    result = audio.generate_audio.run(run_id=101, tts_model="edge-tts")
    audio_path = str(result["audio_path"])
    whisper = SubtitleProvider()
    artifacts = FakeSubtitleService()
    monkeypatch.setattr(subtitles, "_script_service", scripts)
    monkeypatch.setattr(
        subtitles, "_audio_service", SubtitleAudioService(FakeAudioArtifact(audio_path))
    )
    monkeypatch.setattr(subtitles, "_subtitle_service", artifacts)
    monkeypatch.setattr(subtitles, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(subtitles, "record_provider_call", AsyncMock())
    monkeypatch.setattr(
        subtitles,
        "get_default_registry",
        lambda: SubtitleRegistry(
            SubtitleEntry(requires_gpu=False),
            whisper,
        ),
    )
    subtitle_result = subtitles.generate_subtitles.run(run_id=101)
    # Then audio is kept without retry/fallback, invalid alignment is removed, and Whisper succeeds.
    assert result["status"] == subtitle_result["status"] == "success"
    assert len(provider.calls) == 2
    assert [call[0] for call in provider.calls] == ["Opening.", "Ending."]
    assert Path(audio_path).read_bytes() == b"audio" * 200
    assert not Path(f"{audio_path}.words.jsonl").exists()
    assert whisper.calls[0][0] == audio_path
    assert artifacts.calls[0]["model_used"] == "whisper-small"
    assert any(
        record.levelname == "WARNING"
        and record.name == "tasks.generate_audio"
        and "Skipping unusable TTS timing metadata" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("metadata", [None, "not-json"])
def test_opt_in_subtitles_fail_closed_with_unusable_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: str | None,
) -> None:
    # Given opt-in alignment and audio without a trusted timing envelope.
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"audio")
    if metadata is not None:
        Path(f"{path}.words.jsonl").write_text(metadata, encoding="utf-8")
    storage = _make_storage(stage="SUBTITLE_GENERATING")
    artifacts = FakeSubtitleService()
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(subtitles, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        subtitles, "_script_service", FakeScriptService(FakeScriptDraft(markdown_content="one"))
    )
    monkeypatch.setattr(
        subtitles, "_audio_service", SubtitleAudioService(FakeAudioArtifact(str(path)))
    )
    monkeypatch.setattr(subtitles, "_subtitle_service", artifacts)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.get_audio_duration", lambda *_: 1.0
    )
    monkeypatch.setattr(
        subtitles,
        "get_default_registry",
        lambda: SubtitleRegistry(
            SubtitleEntry(provider_type="script_timestamp_aligner", requires_gpu=False),
            ScriptTimestampAligner(),
        ),
    )
    # When explicitly selecting script timestamps, then metadata remains a strict prerequisite.
    with pytest.raises(ProviderError, match="TTS word timing metadata"):
        subtitles.generate_subtitles.run(run_id=101, subtitle_model="script-timestamps")
    assert artifacts.calls == []
