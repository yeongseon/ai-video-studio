"""Count paid calls through the real audio task and runner, using synthetic audio."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from tasks import generate_audio as audio

from .test_audio_section_timeout import AudioCase, audio_case as audio_case
from .terminal_validation_support import runner_case as runner_case
from .terminal_postgres_support import terminal_pool as terminal_pool


@dataclass(slots=True)
class RecordingProvider:
    """Mutable call journal; fail a selected call after writing synthetic partial bytes."""

    error: Exception | None = None
    fail_at: int = 2
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    async def generate(self, text: str, voice: str, params: dict[str, str]) -> None:
        self.calls.append((text, voice, dict(params)))
        Path(params["output_path"]).write_bytes(b"synthetic audio")
        if self.error is not None and len(self.calls) == self.fail_at:
            raise self.error


@pytest.fixture
def paid_case(audio_case: AudioCase, monkeypatch: pytest.MonkeyPatch):
    provider = RecordingProvider()
    authorization = AsyncMock(return_value=True)
    monkeypatch.setattr(audio_case.provider, "generate", provider.generate)
    monkeypatch.setattr(audio, "authorize_owned_provider_attempt", authorization)
    monkeypatch.setattr(
        "creator_service.ffmpeg_service.FFmpegService.concatenate_audio",
        lambda _self, _paths, output: Path(output).write_bytes(b"joined synthetic audio"),
    )
    return audio_case, provider, authorization


ERRORS = [
    pytest.param(RuntimeError("provider bug"), ProviderError, id="runtime"),
    pytest.param(OSError("provider io"), ProviderError, id="os"),
    pytest.param(ValueError("provider value"), ProviderError, id="value"),
    pytest.param(TimeoutError("deadline"), ProviderTimeoutError, id="timeout"),
    pytest.param(ConnectionError("connection"), ProviderTimeoutError, id="connection"),
    pytest.param(RuntimeError("HTTP 429"), RateLimitError, id="429"),
    pytest.param(RuntimeError("Rate Limit reached"), RateLimitError, id="rate-limit"),
    pytest.param(RuntimeError("Too Many Requests"), RateLimitError, id="too-many"),
    pytest.param(ProviderError("already mapped"), ProviderError, id="provider"),
    pytest.param(ProviderTimeoutError("mapped timeout"), ProviderTimeoutError, id="mapped-timeout"),
    pytest.param(RateLimitError("mapped limit"), RateLimitError, id="mapped-limit"),
    pytest.param(SoftTimeLimitExceeded(), SoftTimeLimitExceeded, id="soft-timeout"),
]


@pytest.mark.parametrize("error,expected", ERRORS)
@pytest.mark.parametrize("single_pass", [False, True], ids=["sections", "single-pass"])
def test_provider_failure_never_starts_an_extra_call(
    paid_case, monkeypatch: pytest.MonkeyPatch, error: Exception,
    expected: type[Exception], single_pass: bool,
) -> None:
    # Given a provider failure with a viable subsequent response and exhausted retry budget.
    case, provider, authorization = paid_case
    provider.error = error
    if single_pass:
        provider.fail_at = 1
        monkeypatch.setattr(
            "creator_service.recipe_profile.resolve_quality_profile", Mock(side_effect=KeyError()),
        )
    task = audio.generate_audio
    task.push_request(id="paid-boundary", retries=5, args=(), kwargs={})
    try:
        # When the real task invokes section two (or the only single-pass call).
        with pytest.raises(expected) as raised:
            task.run(case.runner.run_id, tts_model="edge-tts", voice="synthetic-voice")
    finally:
        task.pop_request()
    # Then mapping preserves retry class/identity, with no extra paid attempt and cleanup.
    assert type(raised.value) is expected
    if isinstance(error, (ProviderError, SoftTimeLimitExceeded)):
        assert raised.value is error
    else:
        assert raised.value.__cause__ is error
    expected_texts = ["Opening. Ending."] if single_pass else ["Opening.", "Ending."]
    assert [call[0] for call in provider.calls] == expected_texts
    assert [call[1] for call in provider.calls] == ["synthetic-voice"] * len(expected_texts)
    suffixes = ["full"] if single_pass else ["section:0", "section:1"]
    ids = [call.args[1] for call in authorization.await_args_list]
    assert len(ids) == len(suffixes)
    assert all(value.startswith("paid-boundary:") for value in ids)
    assert all(value.endswith(f":{suffix}") for value, suffix in zip(ids, suffixes, strict=True))
    assert not Path(provider.calls[0][2]["output_path"]).parent.exists()
    case.usage.assert_not_awaited()


@pytest.mark.parametrize("step", ["artifact", "concat", "sidecar"])
@pytest.mark.parametrize("error", [
    OSError("disk 429 rate limit"),
    subprocess.CalledProcessError(1, "ffmpeg"),
    SoftTimeLimitExceeded(),
    KeyError("programming bug"),
])
def test_post_provider_failure_propagates_unchanged(
    paid_case, monkeypatch: pytest.MonkeyPatch, step: str, error: Exception,
) -> None:
    # Given two successful paid sections, followed by a local failure.
    case, provider, authorization = paid_case
    match step:
        case "artifact":
            monkeypatch.setattr(case.artifacts, "create_paragraph_artifact", AsyncMock(side_effect=error))
        case "concat":
            monkeypatch.setattr(
                "creator_service.ffmpeg_service.FFmpegService.concatenate_audio", Mock(side_effect=error),
            )
        case "sidecar":
            # Only exceptions outside the existing metadata-tolerance list escape this seam.
            if isinstance(error, (OSError, subprocess.SubprocessError)):
                monkeypatch.setattr(
                    audio, "_invalidate_word_timing_sidecar", Mock(side_effect=[error, None]),
                )
            else:
                monkeypatch.setattr(audio, "_merge_word_timing_sidecars", Mock(side_effect=error))
    # When Celery traces this delivery without making a real external call.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="local-failure", throw=False,
    )
    # Then the original local error escapes and no full-script attempt is requested.
    assert result.state == "FAILURE"
    assert result.result is error
    assert [call[0] for call in provider.calls] == ["Opening.", "Ending."]
    ids = [call.args[1] for call in authorization.await_args_list]
    assert len(ids) == 2
    assert ids[0].endswith(":section:0") and ids[1].endswith(":section:1")
    assert not Path(provider.calls[0][2]["output_path"]).parent.exists()
    case.usage.assert_not_awaited()


@pytest.mark.parametrize("error", [KeyError("profile"), ValueError("profile"), ImportError("profile")])
def test_preparation_failure_still_allows_one_single_pass(
    paid_case, monkeypatch: pytest.MonkeyPatch, error: Exception,
) -> None:
    # Given profile preparation fails before any provider call.
    case, provider, authorization = paid_case
    monkeypatch.setattr("creator_service.recipe_profile.resolve_quality_profile", Mock(side_effect=error))
    # When the real task processes the structured script.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="preparation-fallback", throw=True,
    )
    # Then exactly one full-script attempt succeeds with the original text.
    assert result.state == "SUCCESS"
    assert [call[0] for call in provider.calls] == ["Opening. Ending."]
    assert authorization.await_count == 1
    assert authorization.await_args.args[1].endswith(":full")
    assert Path(provider.calls[0][2]["output_path"]).is_file()
    case.usage.assert_awaited_once()


@pytest.mark.parametrize("allowed", [[False], [True, False]])
def test_section_spend_refusal_never_falls_back(paid_case, allowed: list[bool]) -> None:
    # Given spend refusal before section one or after one paid section.
    case, provider, authorization = paid_case
    authorization.side_effect = allowed
    # When the actual task requests authorization.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="spend-refused", throw=False,
    )
    # Then refusal remains a ProviderError, never a preparation fallback.
    assert result.state == "FAILURE"
    assert type(result.result) is ProviderError
    assert [call[0] for call in provider.calls] == (["Opening."] if allowed[0] else [])
    ids = [call.args[1] for call in authorization.await_args_list]
    assert len(ids) == len(allowed)
    assert all(value.endswith(f":section:{idx}") for idx, value in enumerate(ids))
    assert not (case.root / str(case.runner.run_id) / "audio" / "spend-refused").exists()


def test_success_keeps_section_rates_and_accounting(paid_case) -> None:
    # Given the normal section plan and an offline provider that writes both outputs.
    from creator_service.recipe_profile import resolve_quality_profile

    case, provider, authorization = paid_case
    profile = resolve_quality_profile("shorts_default")
    # When the real task completes its delivery.
    result = audio.generate_audio.apply(
        args=(case.runner.run_id,), kwargs={"tts_model": "edge-tts"},
        task_id="happy-sections", throw=True,
    )
    # Then section text/rates, attempt ids and final accounting remain section-based.
    assert result.state == "SUCCESS"
    assert [call[0] for call in provider.calls] == ["Opening.", "Ending."]
    assert [call[2]["rate"] for call in provider.calls] == [
        profile.tts_rate_hook, profile.tts_rate_conclusion,
    ]
    ids = [call.args[1] for call in authorization.await_args_list]
    assert len(ids) == 2
    assert ids[0].endswith(":section:0") and ids[1].endswith(":section:1")
    case.usage.assert_awaited_once()
    assert case.usage.await_args.kwargs["reservation_attempt_id"] == ids[-1]
