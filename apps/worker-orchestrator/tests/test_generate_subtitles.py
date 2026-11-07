from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from tasks import generate_subtitles as generate_subtitles_module


@dataclass
class FakeEntry:
    provider_type: str = "faster-whisper"
    endpoint: str = "http://whisper:8200"
    requires_gpu: bool = True
    default_params: dict[str, object] | None = None


class FakeProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def transcribe(self, audio_path: str, params: dict[str, object] | None = None) -> None:
        self.calls.append((audio_path, params))
        if self.error is not None:
            raise self.error


class FakeStorage:
    def __init__(self, runs: dict[int, dict[str, object]] | None = None) -> None:
        self._runs: dict[int, dict[str, object]] = runs or {}
        self.calls: list[tuple[int, dict[str, object]]] = []
        self.cas_calls: list[tuple[int, dict[str, object], frozenset[str]]] = []

    async def get_run(self, run_id: int) -> dict[str, object] | None:
        return self._runs.get(run_id)

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self.cas_calls.append((run_id, updates, expected_stages))
        row = self._runs.get(run_id)
        if row is None:
            return False, None
        if row.get("current_stage") not in expected_stages:
            return False, dict(row)
        self.calls.append((run_id, updates))
        row.update(updates)
        self._runs[run_id] = row
        return True, dict(row)


def _make_storage(run_id: int = 101, stage: str = "AUDIO_GENERATING") -> FakeStorage:
    run_row: dict[str, object] = {"id": run_id, "current_stage": stage}
    return FakeStorage(runs={run_id: run_row})


@dataclass
class FakeSection:
    text: str = "Section text"
    display_text: str | None = None


@dataclass
class FakeScriptDraft:
    markdown_content: str | None = None
    structured_script: list[FakeSection] | None = None


class FakeScriptService:
    def __init__(self, draft: FakeScriptDraft | None = None) -> None:
        self.draft = draft

    async def get_active_draft(self, _run_id: int) -> FakeScriptDraft | None:
        return self.draft


@dataclass
class FakeAudioArtifact:
    path: str


class FakeAudioService:
    def __init__(self, artifact: FakeAudioArtifact | None = None) -> None:
        self.artifact = artifact

    async def get_latest(self, _run_id: int) -> FakeAudioArtifact | None:
        return self.artifact


@dataclass
class FakeSubtitleArtifact:
    id: int
    path: str


class FakeSubtitleService:
    def __init__(self, artifact_id: int = 1) -> None:
        self.artifact_id = artifact_id
        self.calls: list[dict[str, object]] = []

    async def create_artifact(
        self,
        run_id: int,
        path: str,
        *,
        format: str = "srt",
        model_used: str | None = None,
        provider_type: str | None = None,
        storage_provider: str | None = None,
        storage_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> FakeSubtitleArtifact:
        call_data = {
            "run_id": run_id,
            "path": path,
            "format": format,
            "model_used": model_used,
            "provider_type": provider_type,
            "storage_provider": storage_provider,
            "storage_key": storage_key,
        }
        self.calls.append(call_data)
        return FakeSubtitleArtifact(id=self.artifact_id, path=path)


class FakeRegistry:
    def __init__(self, entry: FakeEntry, provider: FakeProvider) -> None:
        self.entry = entry
        self.provider = provider

    def resolve(self, _model_key: str) -> FakeEntry:
        return self.entry

    def get_provider(self, _model_key: str) -> FakeProvider:
        return self.provider


def _patch_registry(monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry) -> None:
    monkeypatch.setattr(generate_subtitles_module, "get_default_registry", lambda: registry)


def _patch_redis(monkeypatch: pytest.MonkeyPatch, redis_client: object) -> None:
    redis_stub = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda _: redis_client))
    monkeypatch.setattr("tasks.task_runner.redis", redis_stub)


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    script_service: FakeScriptService,
    audio_service: FakeAudioService,
    subtitle_service: FakeSubtitleService,
    storage: FakeStorage,
) -> None:
    monkeypatch.setattr(generate_subtitles_module, "_script_service", script_service)
    monkeypatch.setattr(generate_subtitles_module, "_audio_service", audio_service)
    monkeypatch.setattr(generate_subtitles_module, "_subtitle_service", subtitle_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))


def _invoke_task(**kwargs: Any) -> dict[str, object]:
    task = generate_subtitles_module.generate_subtitles
    run_callable = getattr(task, "run", task)
    return run_callable(**kwargs)


def test_generate_subtitles_success(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    entry = FakeEntry(requires_gpu=True, default_params={"temperature": 0.1})
    _patch_registry(monkeypatch, FakeRegistry(entry=entry, provider=provider))

    redis_client = object()
    _patch_redis(monkeypatch, redis_client)

    lock_calls: list[str] = []
    release_calls: list[str] = []
    monkeypatch.setattr(
        "tasks.task_runner.acquire_gpu_lock",
        lambda _, task_id: (lock_calls.append(task_id) or f"{task_id}:fake-token"),
    )
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda _, token: release_calls.append(token.split(":")[0]) or True,
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Hello world script"))
    audio_service = FakeAudioService(
        artifact=FakeAudioArtifact(path="data/artifacts/101/audio/audio.wav")
    )
    subtitle_service = FakeSubtitleService(artifact_id=33)
    storage = _make_storage(run_id=101, stage="AUDIO_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    result = _invoke_task(run_id=101, subtitle_model="whisper-large", subtitle_format="srt")

    assert result["status"] == "success"
    assert result["subtitle_artifact_id"] == 33
    assert result["provider_type"] == "faster-whisper"
    assert result["endpoint"] == "http://whisper:8200"
    assert result["gpu_lock_acquired_at"] is not None
    assert result["gpu_lock_released_at"] is not None
    assert result["subtitle_path"] == "data/artifacts/101/subtitles/subtitles.srt"
    assert result["audio_path"] == "data/artifacts/101/audio/audio.wav"

    assert lock_calls == ["run-101"]
    assert release_calls == ["run-101"]

    assert provider.calls == [
        (
            "data/artifacts/101/audio/audio.wav",
            {
                "temperature": 0.1,
                "format": "srt",
                "output_path": "data/artifacts/101/subtitles/subtitles.srt",
            },
        )
    ]
    assert len(subtitle_service.calls) == 1
    call = subtitle_service.calls[0]
    assert call["run_id"] == 101
    assert call["path"] == "data/artifacts/101/subtitles/subtitles.srt"
    assert call["format"] == "srt"
    assert call["model_used"] == "whisper-large"
    assert call["provider_type"] == "faster-whisper"
    assert call["storage_provider"] == "local"
    assert "storage_key" in call
    assert storage.calls == [(101, {"current_stage": "RENDER_GENERATING", "status": "running"})]
    assert storage.cas_calls[0][2] == frozenset({"AUDIO_GENERATING", "SUBTITLE_GENERATING"})


def test_generate_subtitles_run_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Any"))
    audio_service = FakeAudioService(artifact=None)
    subtitle_service = FakeSubtitleService()
    storage = FakeStorage()
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    with pytest.raises(ValueError, match="Run 999 not found"):
        _invoke_task(run_id=999)

    assert provider.calls == []
    assert storage.calls == []
    assert subtitle_service.calls == []


def test_generate_subtitles_wrong_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Any"))
    audio_service = FakeAudioService(artifact=None)
    subtitle_service = FakeSubtitleService()
    storage = _make_storage(run_id=102, stage="SCRIPT_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    with pytest.raises(ValueError, match="expected one of"):
        _invoke_task(run_id=102)

    assert provider.calls == []
    assert storage.calls == []
    assert subtitle_service.calls == []


def test_generate_subtitles_no_script(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=None)
    audio_service = FakeAudioService(artifact=None)
    subtitle_service = FakeSubtitleService()
    storage = _make_storage(run_id=103, stage="AUDIO_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    with pytest.raises(ValueError, match="No script draft found"):
        _invoke_task(run_id=103)

    assert provider.calls == []
    assert storage.calls == []
    assert subtitle_service.calls == []


def test_generate_subtitles_provider_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(error=RuntimeError("subtitle provider failed"))
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Some script"))
    audio_service = FakeAudioService(
        artifact=FakeAudioArtifact(path="data/artifacts/104/audio/audio.wav")
    )
    subtitle_service = FakeSubtitleService()
    storage = _make_storage(run_id=104, stage="AUDIO_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    with pytest.raises(
        generate_subtitles_module.ProviderError,
        match="Provider failed subtitle generation",
    ):
        _invoke_task(run_id=104)

    assert subtitle_service.calls == []
    assert storage.calls == [(104, {"current_stage": "FAILED", "status": "failed"})]
    assert storage.cas_calls[0][2] == frozenset({"AUDIO_GENERATING", "SUBTITLE_GENERATING"})


def test_generate_subtitles_with_audio_path(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Narration text"))
    audio_service = FakeAudioService(
        artifact=FakeAudioArtifact(path="data/artifacts/105/audio/audio.wav")
    )
    subtitle_service = FakeSubtitleService()
    storage = _make_storage(run_id=105, stage="AUDIO_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    result = _invoke_task(run_id=105, subtitle_format="vtt")

    assert result["status"] == "success"
    assert result["audio_path"] == "data/artifacts/105/audio/audio.wav"
    assert provider.calls[0][0] == "data/artifacts/105/audio/audio.wav"
    params = provider.calls[0][1]
    assert params is not None
    assert params["format"] == "vtt"
    assert params["output_path"] == "data/artifacts/105/subtitles/subtitles.vtt"


def test_generate_subtitles_passes_known_script_to_timestamp_aligner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given explicit selection of the script timestamp aligner.
    provider = FakeProvider()
    entry = FakeEntry(provider_type="script_timestamp_aligner", requires_gpu=False)
    _patch_registry(monkeypatch, FakeRegistry(entry=entry, provider=provider))
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.get_audio_duration", lambda *_: 3.0
    )
    script = "## Heading\n2026년, punctuation—그대로."
    _patch_services(
        monkeypatch,
        FakeScriptService(draft=FakeScriptDraft(markdown_content=script)),
        FakeAudioService(artifact=FakeAudioArtifact(path="data/artifacts/108/audio/audio.mp3")),
        FakeSubtitleService(),
        _make_storage(run_id=108, stage="SUBTITLE_GENERATING"),
    )

    # When subtitles are generated with the opt-in model.
    result = _invoke_task(run_id=108, subtitle_model="script-timestamps")

    # Then the approved script is supplied as alignment ground truth.
    assert result["status"] == "success"
    params = provider.calls[0][1]
    assert params is not None
    assert params["script_text"] == "2026년, punctuation—그대로."


def test_generate_subtitles_rejects_invalid_measured_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given script alignment selected when the duration probe returns no valid bound.
    provider = FakeProvider()
    entry = FakeEntry(provider_type="script_timestamp_aligner", requires_gpu=False)
    _patch_registry(monkeypatch, FakeRegistry(entry=entry, provider=provider))
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.get_audio_duration", lambda *_: float("nan")
    )
    _patch_services(
        monkeypatch,
        FakeScriptService(draft=FakeScriptDraft(markdown_content="Known script")),
        FakeAudioService(artifact=FakeAudioArtifact(path="data/artifacts/109/audio/audio.mp3")),
        FakeSubtitleService(),
        _make_storage(run_id=109, stage="SUBTITLE_GENERATING"),
    )

    # When subtitle generation runs, then the provider is not called without a safe audio bound.
    with pytest.raises(generate_subtitles_module.ProviderError, match="measure audio duration"):
        _invoke_task(run_id=109, subtitle_model="script-timestamps")
    assert provider.calls == []


def test_generate_subtitles_without_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Narration text"))
    audio_service = FakeAudioService(artifact=None)
    subtitle_service = FakeSubtitleService()
    storage = _make_storage(run_id=106, stage="SUBTITLE_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    with pytest.raises(
        RuntimeError, match="No audio artifact found for run 106; cannot transcribe"
    ):
        _invoke_task(run_id=106)

    assert provider.calls == []
    assert subtitle_service.calls == []
    assert storage.calls == [(106, {"current_stage": "FAILED", "status": "failed"})]


class _CASSkipStorage(FakeStorage):
    """FakeStorage variant where conditional_update_run always returns (False, row).

    Simulates a concurrent stage change between the provider call and
    the atomic success/failure transition.
    """

    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
    rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self.cas_calls.append((run_id, updates, expected_stages))
        row = self._runs.get(run_id)
        # Always reject — stage was moved by another worker.
        return False, dict(row) if row else None


def test_generate_subtitles_cas_skip_on_stage_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """When another worker advances the stage during subtitle generation,
    the CAS success transition is skipped gracefully (no error, no stage mutation).
    """
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Narration text"))
    audio_service = FakeAudioService(
        artifact=FakeAudioArtifact(path="data/artifacts/107/audio/audio.wav")
    )
    subtitle_service = FakeSubtitleService(artifact_id=50)

    # Storage starts at AUDIO_GENERATING (passes stage guard) but CAS always rejects.
    run_row: dict[str, object] = {"id": 107, "current_stage": "AUDIO_GENERATING"}
    storage = _CASSkipStorage(runs={107: run_row})
    _patch_services(monkeypatch, script_service, audio_service, subtitle_service, storage)

    result = _invoke_task(run_id=107)

    # Task still succeeds — subtitles were generated and artifact created.
    assert result["status"] == "success"
    assert result["subtitle_artifact_id"] == 50

    # Provider was called normally.
    assert len(provider.calls) == 1

    # Subtitle artifact was saved.
    assert len(subtitle_service.calls) == 1

    # CAS was attempted but the transition was NOT applied.
    assert len(storage.cas_calls) == 1
    assert storage.cas_calls[0][1] == {"current_stage": "RENDER_GENERATING", "status": "running"}
    # No actual updates were applied (calls stays empty).
    assert storage.calls == []
