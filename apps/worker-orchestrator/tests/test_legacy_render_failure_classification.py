"""A failure of the local FFmpeg render in the legacy path is classified by its type, never by its text (#1099)."""

from __future__ import annotations

import errno
import wave
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest
from creator_provider.exceptions import ProviderError, ProviderTimeoutError
from creator_service.audio_service import AudioService
from creator_service.ffmpeg_service import FFmpegService
from creator_service.media_integrity import MediaIntegrityError
from creator_service.render_scratch_monitor import RenderScratchLimitExceeded
from creator_service.script_service import ScriptService
from creator_service.subtitle_service import SubtitleService
from creator_service.visual_asset_service import VisualAssetService
from creator_service.visual_plan_service import VisualPlanService
from PIL import Image
from tasks import render_video
from tests.test_timeline_worker import WorkerFixture, worker as worker


def _seed_legacy_run(worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> int:
    audio, subtitles, visuals = AudioService(), SubtitleService(), VisualAssetService()
    monkeypatch.setattr(render_video, "_audio_service", audio)
    monkeypatch.setattr(render_video, "_subtitle_service", subtitles)
    monkeypatch.setattr(render_video, "_visual_asset_service", visuals)
    monkeypatch.setattr(render_video, "_visual_plan_service", VisualPlanService())
    monkeypatch.setattr(render_video, "_script_service", ScriptService())

    class FailingFFmpeg(FFmpegService):
        def render(self, *_args: object, **_kwargs: object) -> Path:
            raise failure

    monkeypatch.setattr(render_video, "FFmpegService", FailingFFmpeg)
    image = worker.root / "scene.png"
    Image.new("RGB", (96, 160), "red").save(image)
    voice = worker.root / "voice.wav"
    with wave.open(str(voice), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x00\x00" * 4000)
    subtitle = worker.root / "captions.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nLEGACY CAPTION\n")

    async def seed() -> int:
        row = await worker.runs.create_run({"project_id": 11, "current_stage": "RENDER_GENERATING", "status": "running"})
        run_id = int(row["id"])
        await visuals.create_asset(run_id, "scene", str(image))
        await audio.create_artifact(run_id, str(voice))
        await subtitles.create_artifact(run_id, str(subtitle))
        return run_id

    return anyio.run(seed)


def _failure_type(worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> type[BaseException]:
    run_id = _seed_legacy_run(worker, monkeypatch, failure)
    with pytest.raises(BaseException) as raised:
        render_video.render_video.run(run_id=run_id, render_profile="fast_preview")
    return type(raised.value)


LOCAL_ERRORS: dict[str, Callable[[], BaseException]] = {
    "path-with-429": lambda: PermissionError(errno.EACCES, "Permission denied", "/data/artifacts/1429/render/.render_tmp_7_ab12cd34"),
    "words-rate-limit": lambda: OSError("scratch volume: rate limit exceeded"),
    "plain-failure": lambda: RuntimeError("encoder failed"),
}


@pytest.mark.parametrize("make", LOCAL_ERRORS.values(), ids=LOCAL_ERRORS.keys())
def test_a_local_error_is_a_provider_failure_whatever_its_text_says(
    worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch, make: Callable[[], BaseException],
) -> None:
    # Given a local FFmpeg render that fails with an error whose text contains 429 or rate limit words.
    # When the legacy render task runs, then it fails as a plain provider failure and is never retried as a rate limit.
    assert _failure_type(worker, monkeypatch, make()) is ProviderError


TRANSIENT_ERRORS: dict[str, Callable[[], BaseException]] = {
    "timeout": lambda: TimeoutError("timed out"),
    "connection-reset": lambda: ConnectionResetError("reset"),
}


@pytest.mark.parametrize("make", TRANSIENT_ERRORS.values(), ids=TRANSIENT_ERRORS.keys())
def test_a_transient_filesystem_timeout_is_still_retried_as_a_timeout(
    worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch, make: Callable[[], BaseException],
) -> None:
    # Given a local render that fails with a timeout or connection error (a network mount that stalled).
    # When the legacy render task runs, then the recorded decision holds: it is a bounded, retried timeout.
    assert _failure_type(worker, monkeypatch, make()) is ProviderTimeoutError


def test_media_integrity_failure_stays_a_provider_failure(worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a rendered output that fails the integrity check.
    # When the legacy render task runs, then it is reported as a provider failure.
    assert _failure_type(worker, monkeypatch, MediaIntegrityError("duration")) is ProviderError


def test_scratch_limit_failure_passes_through_unchanged(worker: WorkerFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a render that outgrows its scratch allowance.
    # When the legacy render task runs, then the typed failure is not reclassified.
    assert _failure_type(worker, monkeypatch, RenderScratchLimitExceeded(1, 2, "run")) is RenderScratchLimitExceeded
