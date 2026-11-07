"""Exercise section fallback through the real Celery task and common runner."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from billiard.exceptions import SoftTimeLimitExceeded
from celery.exceptions import SoftTimeLimitExceeded as CelerySoftTimeLimitExceeded
from creator_domain.models.script_draft import ScriptSection
from creator_service.audio_service import AudioService, InMemoryAudioStorage
from creator_service.script_service import InMemoryScriptStorage, ScriptService
from creator_provider.tts.edge_tts_provider import EdgeTTSProvider
from tasks import generate_audio as audio
from worker_loop import run_in_worker_loop

from .terminal_postgres_support import terminal_pool as terminal_pool
from .terminal_validation_support import RunnerCase, runner_case as runner_case


@dataclass(frozen=True, slots=True)
class AudioCase:
    runner: RunnerCase
    provider: AsyncMock
    artifacts: AudioService
    root: Path
    usage: AsyncMock


@pytest.fixture
def audio_case(
    runner_case: RunnerCase, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> AudioCase:
    run_in_worker_loop(runner_case.runs.storage.update_run(
        runner_case.run_id, {"current_stage": "AUDIO_GENERATING"},
    ))
    scripts = ScriptService(InMemoryScriptStorage())
    run_in_worker_loop(scripts.save_draft(
        runner_case.run_id, "pasted_json", markdown_content="Opening. Ending.",
        structured_script=[
            ScriptSection(section_id="opening", type="hook", text="Opening."),
            ScriptSection(section_id="ending", type="conclusion", text="Ending."),
        ],
    ))
    artifacts = AudioService(InMemoryAudioStorage())
    provider = AsyncMock()
    entry = SimpleNamespace(
        requires_gpu=False, provider_type="edge_tts", default_params={}, endpoint="offline",
    )
    registry = SimpleNamespace(
        resolve=Mock(return_value=entry), get_provider=Mock(return_value=provider),
    )
    usage = AsyncMock()
    monkeypatch.setattr(audio, "_script_service", scripts)
    monkeypatch.setattr(audio, "_audio_service", artifacts)
    monkeypatch.setattr(audio, "get_default_registry", lambda: registry)
    monkeypatch.setattr(audio, "record_provider_call", usage)
    monkeypatch.setattr(audio, "_ARTIFACT_ROOT", str(tmp_path))
    return AudioCase(runner_case, provider, artifacts, tmp_path, usage)


def test_soft_timeout_escapes_section_without_fallback(audio_case: AudioCase) -> None:
    # Given the actual billiard exception on the first section, then a viable fallback.
    case = audio_case
    timeout = SoftTimeLimitExceeded()
    assert SoftTimeLimitExceeded is CelerySoftTimeLimitExceeded
    case.provider.generate.side_effect = [timeout, None]
    audio.generate_audio.push_request(id="audio-timeout", retries=0, args=(), kwargs={})
    try:
        # When the real task/runner executes the section.
        with pytest.raises(SoftTimeLimitExceeded) as raised:
            audio.generate_audio.run(case.runner.run_id, tts_model="edge-tts")
    finally:
        audio.generate_audio.pop_request()
    # Then the same timeout escapes without another provider call or persisted output.
    assert raised.value is timeout
    case.provider.generate.assert_awaited_once()
    case.usage.assert_not_awaited()
    assert run_in_worker_loop(case.artifacts.storage.list_by_run(case.runner.run_id)) == []
    assert run_in_worker_loop(case.artifacts.storage.list_by_run_sections(case.runner.run_id)) == []
    saved = run_in_worker_loop(case.runner.runs.storage.get_run(case.runner.run_id))
    assert (saved["current_stage"], saved["status"]) == ("FAILED", "failed")


def test_edge_sdk_timeout_reaches_audio_task_without_fallback(
    audio_case: AudioCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real Edge adapter whose SDK save raises a soft deadline.
    case = audio_case
    calls: list[tuple[str, str | None]] = []

    class FakeCommunicate:
        def __init__(self, **_kwargs: str) -> None:
            pass

        async def save(self, path: str, metadata_path: str | None = None) -> None:
            calls.append((path, metadata_path))
            raise SoftTimeLimitExceeded()

    provider = EdgeTTSProvider(endpoint="", model_key="edge-tts")
    monkeypatch.setattr("creator_provider.tts.edge_tts_provider.edge_tts.Communicate", FakeCommunicate)
    monkeypatch.setenv("ARTIFACT_ROOT", str(case.root))
    monkeypatch.setattr(audio, "get_default_registry", lambda: SimpleNamespace(
        resolve=Mock(return_value=SimpleNamespace(
            requires_gpu=False, provider_type="edge_tts", default_params={}, endpoint="offline",
        )),
        get_provider=Mock(return_value=provider),
    ))

    # When the actual Celery task and runner invoke the adapter's SDK boundary.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="edge-sdk-timeout", throw=False,
    )

    # Then the deadline identity and single invocation survive without fallback.
    assert result.state == "FAILURE"
    assert isinstance(result.result, SoftTimeLimitExceeded)
    output_path = (
        case.root / str(case.runner.run_id) / "audio" / "edge-sdk-timeout" / "section_0.mp3"
    )
    assert calls == [(str(output_path), f"{output_path}.words.jsonl")]
    case.usage.assert_not_awaited()


@pytest.mark.parametrize("error", [
    RuntimeError("section unavailable"),
])
def test_ordinary_section_error_keeps_legacy_single_pass_fallback(
    audio_case: AudioCase, error: Exception,
) -> None:
    # Given a section error followed by a successful single-pass provider result.
    case = audio_case
    case.provider.generate.side_effect = [error, None]
    output = case.root / str(case.runner.run_id) / "audio" / "audio.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"offline audio" * 100)
    # When the real task executes normally.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="audio-fallback", throw=True,
    )
    # Then the legacy fallback succeeds and finalizes both task and run.
    assert result.state == "SUCCESS"
    assert case.provider.generate.await_count == 2
    section_call, fallback_call = case.provider.generate.await_args_list
    assert section_call.args == ("Opening.",)
    assert fallback_call.args == ("Opening. Ending.",)
    assert Path(section_call.kwargs["params"]["output_path"]).name == "section_0.mp3"
    assert Path(fallback_call.kwargs["params"]["output_path"]).name == "audio.mp3"
    tracked = run_in_worker_loop(case.runner.tracking.list_run_tasks(case.runner.run_id))
    assert [task.status for task in tracked] == ["success"]
    saved = run_in_worker_loop(case.runner.runs.storage.get_run(case.runner.run_id))
    assert (saved["current_stage"], saved["status"]) == ("SUBTITLE_GENERATING", "running")


@pytest.mark.parametrize("stage,status", [
    ("AUDIO_GENERATING", "cancelled"), ("SUBTITLE_GENERATING", "running"),
])
def test_section_timeout_preserves_concurrent_run_state(
    audio_case: AudioCase, stage: str, status: str,
) -> None:
    # Given a cancellation or stage advance during the first provider call.
    case = audio_case

    async def interrupt(*_args: str, **_kwargs: str) -> None:
        await case.runner.runs.storage.update_run(
            case.runner.run_id, {"current_stage": stage, "status": status},
        )
        raise SoftTimeLimitExceeded()

    case.provider.generate.side_effect = interrupt
    # When the actual runner receives the timeout.
    with pytest.raises(SoftTimeLimitExceeded):
        audio.generate_audio.run(case.runner.run_id, tts_model="edge-tts")
    # Then no fallback runs and the caller's conditional failure policy is preserved.
    case.provider.generate.assert_awaited_once()
    saved = run_in_worker_loop(case.runner.runs.storage.get_run(case.runner.run_id))
    assert (saved["current_stage"], saved["status"]) == (stage, status)


def test_celery_marks_section_timeout_delivery_failed(
    audio_case: AudioCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a soft timeout with retry budget available and an offline DLQ boundary.
    case = audio_case
    case.provider.generate.side_effect = [SoftTimeLimitExceeded(), None]
    dlq = Mock()
    monkeypatch.setattr("celery_app._record_failed_task_to_dlq", dlq)
    # When Celery's actual trace processes the exception (not just task.run).
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="audio-celery-timeout", throw=False,
    )
    # Then this is a failed delivery, not success or a provider retry.
    assert result.state == "FAILURE"
    assert isinstance(result.result, SoftTimeLimitExceeded)
    case.provider.generate.assert_awaited_once()
    dlq.assert_called_once()
    assert isinstance(dlq.call_args.kwargs["exception"], SoftTimeLimitExceeded)


def test_celery_timeout_finalizes_owned_task_tracking(audio_case: AudioCase) -> None:
    # Given a claimed delivery interrupted in the first section.
    case = audio_case
    case.provider.generate.side_effect = SoftTimeLimitExceeded()
    # When Celery records the failed delivery.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="audio-tracking-timeout", throw=False,
    )
    # Then the task row is terminal rather than stuck running.
    assert result.state == "FAILURE"
    tracked = run_in_worker_loop(case.runner.tracking.list_run_tasks(case.runner.run_id))
    assert [(task.status, task.error_code) for task in tracked] == [("failed", "INTERNAL")]


def test_celery_timeout_does_not_overwrite_revoked_task(audio_case: AudioCase) -> None:
    # Given a task revoked while its provider is executing.
    case = audio_case

    async def revoke_then_timeout(*_args: str, **_kwargs: str) -> None:
        await case.runner.tracking.mark_revoked("audio-revoked-timeout")
        raise SoftTimeLimitExceeded()

    case.provider.generate.side_effect = revoke_then_timeout
    # When its original delivery times out.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="audio-revoked-timeout", throw=False,
    )
    # Then the delivery fails but the revoked task row is not overwritten.
    assert result.state == "FAILURE"
    tracked = run_in_worker_loop(case.runner.tracking.list_run_tasks(case.runner.run_id))
    assert [task.status for task in tracked] == ["revoked"]


def test_timeout_removes_only_its_delivery_audio(audio_case: AudioCase) -> None:
    # Given another delivery's complete audio and this delivery's partial output.
    case = audio_case
    other = case.root / str(case.runner.run_id) / "audio" / "other-delivery" / "audio.mp3"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"complete")
    generated: list[Path] = []

    async def interrupted(*_args: str, **kwargs: object) -> None:
        params = kwargs["params"]
        assert isinstance(params, dict)
        path = Path(params["output_path"])
        generated.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial")
        raise SoftTimeLimitExceeded()

    case.provider.generate.side_effect = interrupted
    # When a section times out after writing some bytes.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="current-delivery", throw=False,
    )

    # Then only the current delivery's partial output is removed.
    assert result.state == "FAILURE"
    assert len(generated) == 1
    assert generated[0].parent.name == "current-delivery"
    assert not generated[0].exists()
    assert other.read_bytes() == b"complete"


def test_cleanup_failure_does_not_replace_soft_timeout(
    audio_case: AudioCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a partial output and a cleanup error after the provider deadline.
    case = audio_case
    timeout = SoftTimeLimitExceeded()

    async def interrupted(*_args: str, **kwargs: object) -> None:
        params = kwargs["params"]
        assert isinstance(params, dict)
        Path(params["output_path"]).write_bytes(b"partial")
        raise timeout

    case.provider.generate.side_effect = interrupted

    def failed_cleanup(_path: Path) -> None:
        raise OSError("cleanup failed")

    monkeypatch.setattr(audio.shutil, "rmtree", failed_cleanup)
    # When the worker handles the timeout and fails to clean its own directory.
    case_task = audio.generate_audio
    case_task.push_request(id="audio-cleanup-failure", retries=0, args=(), kwargs={})
    try:
        with pytest.raises(SoftTimeLimitExceeded) as raised:
            case_task.run(case.runner.run_id, tts_model="edge-tts")
    finally:
        case_task.pop_request()

    # Then the timeout identity remains the reported failure.
    assert raised.value is timeout


def test_symlink_delivery_directory_cannot_write_outside_artifacts(audio_case: AudioCase) -> None:
    # Given a delivery directory redirected to another task's existing output.
    case = audio_case
    other = case.root / "other-delivery"
    other.mkdir()
    marker = other / "audio.mp3"
    marker.write_bytes(b"complete")
    link = case.root / str(case.runner.run_id) / "audio" / "symlink-delivery"
    link.parent.mkdir(parents=True)
    link.symlink_to(other, target_is_directory=True)

    # When the task attempts to stage its own audio.
    task = audio.generate_audio
    task.push_request(id="symlink-delivery", retries=0, args=(), kwargs={})
    try:
        with pytest.raises(ValueError, match="symlink|artifact"):
            task.run(case.runner.run_id, tts_model="edge-tts")
    finally:
        task.pop_request()

    # Then the pre-existing output stays untouched and no provider is invoked.
    assert marker.read_bytes() == b"complete"
    case.provider.generate.assert_not_awaited()
