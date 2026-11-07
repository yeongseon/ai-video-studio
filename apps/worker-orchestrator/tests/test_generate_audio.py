from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tasks import generate_audio as generate_audio_module


@dataclass
class FakeEntry:
    provider_type: str = "qwen_tts"
    endpoint: str = "http://tts-qwen3:8100"
    requires_gpu: bool = True
    default_params: dict[str, object] | None = None


class FakeProvider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []

    async def generate(
        self,
        text: str,
        voice: str = "default",
        params: dict[str, object] | None = None,
    ) -> None:
        self.calls.append((text, voice, params))
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


def _make_storage(run_id: int = 101, stage: str = "VISUAL_ASSET_REVIEW") -> FakeStorage:
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
    id: int
    path: str


class FakeAudioService:
    def __init__(self, artifact_id: int = 1) -> None:
        self.artifact_id = artifact_id
        self.calls: list[dict[str, object]] = []

    async def create_artifact(
        self,
        run_id: int,
        path: str,
        *,
        model_used: str | None = None,
        provider_type: str | None = None,
        voice: str | None = None,
        storage_provider: str | None = None,
        storage_key: str | None = None,
        idempotency_key: str | None = None,
    ) -> FakeAudioArtifact:
        call_data = {
            "run_id": run_id,
            "path": path,
            "model_used": model_used,
            "provider_type": provider_type,
            "voice": voice,
            "storage_provider": storage_provider,
            "storage_key": storage_key,
        }
        self.calls.append(call_data)
        return FakeAudioArtifact(id=self.artifact_id, path=path)


class FakeRegistry:
    def __init__(self, entry: FakeEntry, provider: FakeProvider) -> None:
        self.entry = entry
        self.provider = provider

    def resolve(self, _model_key: str) -> FakeEntry:
        return self.entry

    def get_provider(self, _model_key: str) -> FakeProvider:
        return self.provider


def _patch_registry(monkeypatch: pytest.MonkeyPatch, registry: FakeRegistry) -> None:
    monkeypatch.setattr(generate_audio_module, "get_default_registry", lambda: registry)


def _patch_redis(monkeypatch: pytest.MonkeyPatch, redis_client: object) -> None:
    redis_stub = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda _: redis_client))
    monkeypatch.setattr("tasks.task_runner.redis", redis_stub)


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    script_service: FakeScriptService,
    audio_service: FakeAudioService,
    storage: FakeStorage,
) -> None:
    monkeypatch.setattr(generate_audio_module, "_script_service", script_service)
    monkeypatch.setattr(generate_audio_module, "_audio_service", audio_service)
    monkeypatch.setattr("tasks.task_runner._run_service", SimpleNamespace(storage=storage))


def _invoke_task(**kwargs: Any) -> dict[str, object]:
    task = generate_audio_module.generate_audio
    run_callable = getattr(task, "run", task)
    return run_callable(**kwargs)


def test_merge_word_timing_sidecars_rebases_section_offsets(tmp_path: Path) -> None:
    # Given two section-audio timing sidecars and measured section durations.
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    for path, text in ((first, "하나"), (second, "둘")):
        Path(f"{path}.words.jsonl").write_text(
            json.dumps(
                {
                    "format": "short-form-studio/tts-word-timings",
                    "version": 2,
                    "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "script_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "script_text": text,
                    "words": [
                        {
                            "type": "WordBoundary",
                            "offset": 0,
                            "duration": 10_000_000,
                            "text": text,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    class FakeFFmpeg:
        def get_audio_duration(self, path: str) -> float:
            return 1.5 if path == str(first) else 2.0

    output = tmp_path / "full.mp3"
    output.write_bytes(b"concatenated")

    # When timing metadata is merged for the concatenated audio.
    generate_audio_module._merge_word_timing_sidecars(
        [(str(first), "하나"), (str(second), "둘")], str(output), FakeFFmpeg()
    )

    # Then the second section starts after the measured first-section duration.
    envelope = json.loads(Path(f"{output}.words.jsonl").read_text(encoding="utf-8"))
    rows = envelope["words"]
    assert [row["offset"] for row in rows] == [0, 15_000_000]
    assert envelope["format"] == "short-form-studio/tts-word-timings"
    assert envelope["version"] == 2
    assert envelope["audio_sha256"] == hashlib.sha256(b"concatenated").hexdigest()
    assert envelope["script_sha256"] == hashlib.sha256("하나\n둘".encode()).hexdigest()


@pytest.mark.parametrize("replacement", ["audio", "script"])
def test_merge_word_timing_sidecars_rejects_replaced_section_before_publish(
    tmp_path: Path,
    replacement: str,
) -> None:
    # Given one section sidecar bound to the section's original audio and exact TTS input.
    section = tmp_path / "section.mp3"
    section.write_bytes(b"original audio")
    section_text = "same"
    Path(f"{section}.words.jsonl").write_text(
        json.dumps(
            {
                "format": "short-form-studio/tts-word-timings",
                "version": 2,
                "audio_sha256": hashlib.sha256(section.read_bytes()).hexdigest(),
                "script_sha256": hashlib.sha256(section_text.encode()).hexdigest(),
                "script_text": section_text,
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
    if replacement == "audio":
        section.write_bytes(b"replacement audio")
    else:
        section_text = "different script"

    class FakeFFmpeg:
        def get_audio_duration(self, _path: str) -> float:
            return 1.0

    output = tmp_path / "full.mp3"
    output.write_bytes(b"concatenated")
    final_sidecar = Path(f"{output}.words.jsonl")
    final_sidecar.write_text("stale", encoding="utf-8")

    # When merge sees substituted section provenance, then no trusted final sidecar is published.
    with pytest.raises(generate_audio_module.ProviderError):
        generate_audio_module._merge_word_timing_sidecars(
            [(str(section), section_text)], str(output), FakeFFmpeg()
        )
    assert not final_sidecar.exists()


def test_invalidate_word_timing_sidecar_removes_stale_alignment(tmp_path: Path) -> None:
    # Given a full-audio sidecar left by a failed per-section attempt.
    audio_path = tmp_path / "audio.mp3"
    sidecar = Path(f"{audio_path}.words.jsonl")
    sidecar.write_text('{"text":"stale"}\n', encoding="utf-8")

    # When the worker switches to single-pass fallback.
    generate_audio_module._invalidate_word_timing_sidecar(str(audio_path))

    # Then stale timings cannot be paired with replacement audio.
    assert not sidecar.exists()


def test_generate_audio_success(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    entry = FakeEntry(requires_gpu=False, default_params={"temperature": 0.2})
    _patch_registry(monkeypatch, FakeRegistry(entry=entry, provider=provider))

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Hello world script"))
    audio_service = FakeAudioService(artifact_id=33)
    storage = _make_storage(run_id=101, stage="VISUAL_ASSET_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    result = _invoke_task(run_id=101, tts_model="qwen3-tts", voice="en_US-lessac-medium")

    assert result["status"] == "success"
    assert result["audio_artifact_id"] == 33
    assert result["provider_type"] == "qwen_tts"
    assert result["endpoint"] == "http://tts-qwen3:8100"
    assert result["audio_path"] == "data/artifacts/101/audio/run-101/audio.wav"

    assert provider.calls == [
        (
            "Hello world script",
            "en_US-lessac-medium",
            {
                "temperature": 0.2,
                "output_path": "data/artifacts/101/audio/run-101/audio.wav",
            },
        )
    ]
    params = provider.calls[0][2]
    assert params is not None
    assert "voice" not in params

    assert len(audio_service.calls) == 1
    call = audio_service.calls[0]
    assert call["run_id"] == 101
    assert call["path"] == "data/artifacts/101/audio/run-101/audio.wav"
    assert call["model_used"] == "qwen3-tts"
    assert call["provider_type"] == "qwen_tts"
    assert call["voice"] == "en_US-lessac-medium"
    assert call["storage_provider"] == "local"
    assert "storage_key" in call
    assert storage.calls == [(101, {"current_stage": "SUBTITLE_GENERATING", "status": "running"})]
    assert storage.cas_calls[0][2] == frozenset({"VISUAL_ASSET_REVIEW", "AUDIO_GENERATING"})


def test_generate_audio_run_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Any"))
    audio_service = FakeAudioService()
    storage = FakeStorage()
    _patch_services(monkeypatch, script_service, audio_service, storage)

    with pytest.raises(ValueError, match="Run 999 not found"):
        _invoke_task(run_id=999)

    assert provider.calls == []
    assert storage.calls == []
    assert audio_service.calls == []


def test_generate_audio_wrong_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Any"))
    audio_service = FakeAudioService()
    storage = _make_storage(run_id=102, stage="SCRIPT_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    with pytest.raises(ValueError, match="expected one of"):
        _invoke_task(run_id=102)

    assert provider.calls == []
    assert storage.calls == []
    assert audio_service.calls == []


def test_generate_audio_no_script(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=None)
    audio_service = FakeAudioService()
    storage = _make_storage(run_id=103, stage="VISUAL_ASSET_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    with pytest.raises(ValueError, match="No script draft found"):
        _invoke_task(run_id=103)

    assert provider.calls == []
    assert storage.calls == []
    assert audio_service.calls == []


def test_generate_audio_provider_failure_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(error=RuntimeError("audio provider failed"))
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Some script"))
    audio_service = FakeAudioService()
    storage = _make_storage(run_id=104, stage="VISUAL_ASSET_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    with pytest.raises(
        generate_audio_module.ProviderError, match="Provider failed audio generation"
    ):
        _invoke_task(run_id=104)

    assert audio_service.calls == []
    assert storage.calls == [(104, {"current_stage": "FAILED", "status": "failed"})]
    assert storage.cas_calls[0][2] == frozenset({"VISUAL_ASSET_REVIEW", "AUDIO_GENERATING"})


def test_generate_audio_preserves_provider_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(error=generate_audio_module.ProviderTimeoutError("provider timeout"))
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Some script"))
    audio_service = FakeAudioService()
    storage = _make_storage(run_id=108, stage="VISUAL_ASSET_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    with pytest.raises(generate_audio_module.ProviderTimeoutError, match="provider timeout"):
        _invoke_task(run_id=108)

    assert audio_service.calls == []
    assert storage.calls == []


def test_generate_audio_with_gpu_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    entry = FakeEntry(requires_gpu=True)
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

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Lock script"))
    audio_service = FakeAudioService(artifact_id=61)
    storage = _make_storage(run_id=105, stage="VISUAL_ASSET_REVIEW")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    result = _invoke_task(run_id=105)

    assert result["status"] == "success"
    assert result["gpu_lock_acquired_at"] is not None
    assert result["gpu_lock_released_at"] is not None
    assert lock_calls == ["run-105"]
    assert release_calls == ["run-105"]


def test_generate_audio_without_gpu_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    entry = FakeEntry(requires_gpu=False)
    _patch_registry(monkeypatch, FakeRegistry(entry=entry, provider=provider))

    monkeypatch.setattr(
        "tasks.task_runner.acquire_gpu_lock",
        lambda *_: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    monkeypatch.setattr(
        "tasks.task_runner.release_gpu_lock",
        lambda *_: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="No lock script"))
    audio_service = FakeAudioService(artifact_id=71)
    storage = _make_storage(run_id=106, stage="AUDIO_GENERATING")
    _patch_services(monkeypatch, script_service, audio_service, storage)

    result = _invoke_task(run_id=106, voice="custom-voice")

    assert result["status"] == "success"
    assert result["gpu_lock_acquired_at"] is None
    assert result["gpu_lock_released_at"] is None
    assert provider.calls[0][1] == "custom-voice"


class _CASSkipStorage(FakeStorage):
    async def conditional_update_run(
        self,
        run_id: int,
        updates: dict[str, object],
        expected_stages: frozenset[str],
        rejected_statuses: frozenset[str] | None = None,
    ) -> tuple[bool, dict[str, object] | None]:
        self.cas_calls.append((run_id, updates, expected_stages))
        row = self._runs.get(run_id)
        return False, dict(row) if row else None


def test_generate_audio_cas_skip_on_stage_change(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider()
    _patch_registry(
        monkeypatch, FakeRegistry(entry=FakeEntry(requires_gpu=False), provider=provider)
    )

    script_service = FakeScriptService(draft=FakeScriptDraft(markdown_content="Narration text"))
    audio_service = FakeAudioService(artifact_id=50)

    run_row: dict[str, object] = {"id": 107, "current_stage": "VISUAL_ASSET_REVIEW"}
    storage = _CASSkipStorage(runs={107: run_row})
    _patch_services(monkeypatch, script_service, audio_service, storage)

    result = _invoke_task(run_id=107)

    assert result["status"] == "success"
    assert result["audio_artifact_id"] == 50
    assert len(provider.calls) == 1
    assert len(audio_service.calls) == 1
    assert len(storage.cas_calls) == 1
    assert storage.cas_calls[0][1] == {"current_stage": "SUBTITLE_GENERATING", "status": "running"}
    assert storage.calls == []
