# pyright: reportMissingImports=false
# ruff: noqa: E402

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from celery_app import celery_app
from creator_domain.models.stage import RunStage
from creator_domain.sanitize import UnsafePathComponent, validate_artifact_path
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from creator_provider.registry import get_default_registry
from creator_provider.word_timing_envelope import read_bound_script
from creator_service.audio_service import audio_service as _audio_service
from creator_service.cost_config import COST_SUBTITLE_GENERATION
from creator_service.script_service import script_service as _script_service
from creator_service.subtitle_service import subtitle_service as _subtitle_service
from creator_service.telemetry import trace_task
from creator_service.usage_service import authorize_owned_provider_attempt, record_provider_call
from tasks.task_runner import GpuLockContext, TaskContext, TaskResult, TaskRunnerConfig, run_task
from tasks.hard_timeout import production_task
from tasks.task_execution import TaskInputError

logger = logging.getLogger(__name__)
_ARTIFACT_ROOT = os.getenv("ARTIFACT_ROOT", "data/artifacts")


@celery_app.task(
    bind=True,
    base=production_task(
        frozenset({RunStage.AUDIO_GENERATING.value, RunStage.SUBTITLE_GENERATING.value})
    ),
    autoretry_for=(ProviderTimeoutError, RateLimitError),
    retry_backoff=30,
    retry_jitter=True,
    max_retries=5,
    soft_time_limit=300,
    time_limit=360,
    name="generate_subtitles",
)
@trace_task("generate_subtitles")
def generate_subtitles(
    self,
    run_id: int,
    subtitle_model: str = "whisper-small",
    subtitle_format: str = "srt",
) -> dict[str, object]:
    config = TaskRunnerConfig(
        task_name="generate_subtitles",
        allowed_stages=frozenset({RunStage.AUDIO_GENERATING, RunStage.SUBTITLE_GENERATING}),
        safe_stages=frozenset(
            {RunStage.AUDIO_GENERATING.value, RunStage.SUBTITLE_GENERATING.value}
        ),
        success_stage=RunStage.RENDER_GENERATING.value,
    )

    async def execute(ctx: TaskContext) -> TaskResult:
        if subtitle_format not in ("srt", "vtt"):
            raise TaskInputError(
                f"Invalid subtitle_format: {subtitle_format!r}. Must be 'srt' or 'vtt'."
            )

        draft = await _script_service.get_active_draft(run_id)
        if draft is None:
            raise TaskInputError(f"No script draft found for run {run_id}")

        script_text = (draft.markdown_content or "").strip()
        if not script_text and draft.structured_script:
            script_text = "\n".join(
                (section.display_text or section.text).strip()
                for section in draft.structured_script
                if (section.display_text or section.text).strip()
            )
        if not script_text:
            raise TaskInputError(f"Script draft for run {run_id} has no content")

        audio_artifact = await _audio_service.get_latest(run_id)
        audio_path = audio_artifact.path if audio_artifact else None
        if not audio_path:
            raise RuntimeError(f"No audio artifact found for run {run_id}; cannot transcribe")
        try:
            validate_artifact_path(audio_path, _ARTIFACT_ROOT)
        except UnsafePathComponent as exc:
            raise RuntimeError(
                f"Unsafe audio artifact path for run {run_id}: {audio_path!r}"
            ) from exc

        registry = get_default_registry()
        entry = registry.resolve(subtitle_model)
        provider = registry.get_provider(subtitle_model)

        gpu_lock = GpuLockContext(ctx.task_id)
        if entry.requires_gpu:
            gpu_lock.acquire()

        subtitle_path = f"{_ARTIFACT_ROOT}/{run_id}/subtitles/subtitles.{subtitle_format}"
        try:
            os.makedirs(os.path.dirname(subtitle_path), exist_ok=True)
            params = dict(entry.default_params or {})
            params["format"] = subtitle_format
            params["output_path"] = subtitle_path
            if entry.provider_type == "script_timestamp_aligner":
                try:
                    from creator_service.ffmpeg_service import FFmpegService

                    measured_duration = FFmpegService().get_audio_duration(audio_path)
                    if not math.isfinite(measured_duration) or measured_duration <= 0:
                        raise ValueError
                    params["audio_duration_seconds"] = measured_duration
                except (OSError, RuntimeError, TypeError, ValueError):
                    raise ProviderError(
                        f"Could not measure audio duration for script alignment for run {run_id}"
                    ) from None
                params["script_text"] = read_bound_script(Path(audio_path))
            try:
                attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}:stt"
                if not await authorize_owned_provider_attempt(
                    ctx.reservation_owner_id,
                    attempt_id,
                    COST_SUBTITLE_GENERATION,
                ):
                    raise ProviderError("Provider attempt exceeds approved spend")
                ctx.mark_provider_started()
                await provider.transcribe(audio_path, params=params)
            except ProviderError:
                raise
            except (TimeoutError, ConnectionError) as exc:
                raise ProviderTimeoutError(
                    f"Provider timed out during subtitle generation for run {run_id}"
                ) from exc
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if "429" in message or "rate limit" in message or "too many requests" in message:
                    raise RateLimitError(
                        f"Provider rate limited subtitle generation for run {run_id}"
                    ) from exc
                raise ProviderError(
                    f"Provider failed subtitle generation for run {run_id}"
                ) from exc
        finally:
            if entry.requires_gpu:
                gpu_lock.release()

        try:
            await record_provider_call(
                run_id,
                entry.provider_type,
                subtitle_model,
                "stt",
                cost_usd=COST_SUBTITLE_GENERATION,
                workspace_id=ctx.workspace_id,
                project_id=ctx.project_id,
                idempotency_key=ctx.task_id,
                reservation_owner_id=ctx.reservation_owner_id,
                reservation_attempt_id=attempt_id,
            )
            ctx.mark_provider_accounted()
        except Exception:
            if ctx.reservation_owner_id is not None:
                raise
            logger.warning("Failed to record provider usage", exc_info=True)

        from creator_service.artifact_storage_integration import store_artifact_file

        uploaded = store_artifact_file(run_id, subtitle_path, f"application/{subtitle_format}")
        artifact = await _subtitle_service.create_artifact(
            run_id=run_id,
            path=subtitle_path,
            format=subtitle_format,
            model_used=subtitle_model,
            provider_type=entry.provider_type,
            storage_provider=uploaded.storage_provider,
            storage_key=uploaded.key,
            idempotency_key=ctx.task_id,
        )

        return TaskResult(
            status="success",
            extra={
                "subtitle_model": subtitle_model,
                "subtitle_format": subtitle_format,
                "provider_type": entry.provider_type,
                "endpoint": entry.endpoint,
                "gpu_lock_acquired_at": gpu_lock.acquired_at,
                "gpu_lock_released_at": gpu_lock.released_at,
                "subtitle_artifact_id": artifact.id,
                "subtitle_path": artifact.path,
                "audio_path": audio_path,
            },
        )

    return run_task(self, run_id, config, execute)
