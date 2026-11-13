# pyright: reportMissingImports=false
# ruff: noqa: E402

from __future__ import annotations

import logging
import os
import wave
from decimal import Decimal
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from celery_app import celery_app
from creator_domain.models.stage import RunStage
from creator_domain.sanitize import sanitize_path_component
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from creator_provider.registry import get_default_registry
from creator_service.audio_service import audio_service as _audio_service
from creator_service.cost_config import COST_PARAGRAPH_AUDIO
from creator_service.script_service import script_service as _script_service
from creator_service.telemetry import trace_task
from creator_service.usage_service import authorize_owned_provider_attempt, record_provider_call
from creator_service.spend_quote import MAX_AUDIO_DURATION_SECONDS, audio_reservation_quote
from tasks.task_runner import GpuLockContext, TaskContext, TaskResult, TaskRunnerConfig, run_task
from tasks.hard_timeout import production_task
from tasks.task_execution import TaskInputError

logger = logging.getLogger(__name__)
_ARTIFACT_ROOT = os.getenv("ARTIFACT_ROOT", "data/artifacts")

def _get_wav_duration_seconds(path: str) -> float | None:
    try:
        with wave.open(path, "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return wav_file.getnframes() / float(frame_rate)
    except Exception:
        logger.warning("Could not determine audio duration for %s", path, exc_info=True)
        return None

@celery_app.task(
    bind=True,
    base=production_task(frozenset({RunStage.AUDIO_GENERATING.value, RunStage.SUBTITLE_GENERATING.value})),
    autoretry_for=(ProviderTimeoutError, RateLimitError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
    soft_time_limit=300,
    time_limit=360,
    name="generate_paragraph_audio",
)
@trace_task("generate_paragraph_audio")
def generate_paragraph_audio(
    self,
    run_id: int,
    section_id: str,
    tts_model: str = "qwen3-tts",
    voice: str = "default",
) -> dict[str, object]:
    config = TaskRunnerConfig(
        task_name="generate_paragraph_audio",
        allowed_stages=frozenset({RunStage.AUDIO_GENERATING, RunStage.SUBTITLE_GENERATING}),
        safe_stages=frozenset(
            {RunStage.AUDIO_GENERATING.value, RunStage.SUBTITLE_GENERATING.value}
        ),
        success_stage=None,
        requires_free_space=True,
    )

    async def execute(ctx: TaskContext) -> TaskResult:
        draft = await _script_service.get_active_draft(run_id)
        if draft is None:
            raise TaskInputError(f"No script draft found for run {run_id}")

        sections = draft.structured_script or []
        section_text: str | None = None
        for section in sections:
            if section.section_id == section_id:
                section_text = (section.display_text or section.text or "").strip()
                break
        if section_text is None:
            raise TaskInputError(f"Section '{section_id}' not found for run {run_id}")

        registry = get_default_registry()
        entry = registry.resolve(tts_model)
        provider = registry.get_provider(tts_model)
        delivery_cost = audio_reservation_quote(
            duration_seconds=MAX_AUDIO_DURATION_SECONDS,
            max_retries=0,
            cost_per_second=Decimal("0.0003"),
        )
        lock_id = f"para-{run_id}-{section_id}"
        gpu_lock = GpuLockContext(ctx.task_id)
        if entry.requires_gpu:
            gpu_lock.acquire(lock_id=lock_id)

        safe_section_id = sanitize_path_component(section_id, label="section_id")
        delivery_dir = Path(_ARTIFACT_ROOT) / str(run_id) / "audio" / sanitize_path_component(
            ctx.task_id, label="task_id",
        )
        audio_path = str(delivery_dir / f"{safe_section_id}.wav")
        try:
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)
            params = dict(entry.default_params or {})
            params["output_path"] = audio_path
            attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}"
            try:
                if not await authorize_owned_provider_attempt(
                    ctx.reservation_owner_id,
                    attempt_id,
                    delivery_cost,
                ):
                    raise ProviderError("Provider attempt exceeds approved spend")
                ctx.mark_provider_started()
                await provider.generate(section_text, voice=voice, params=params)
            except ProviderError:
                raise
            except (TimeoutError, ConnectionError) as exc:
                raise ProviderTimeoutError(
                    "Provider timed out during paragraph audio generation "
                    f"for run {run_id} section {section_id}"
                ) from exc
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                message = str(exc).lower()
                if "429" in message or "rate limit" in message or "too many requests" in message:
                    raise RateLimitError(
                        "Provider rate limited paragraph audio generation "
                        f"for run {run_id} section {section_id}"
                    ) from exc
                raise ProviderError(
                    "Provider failed paragraph audio generation "
                    f"for run {run_id} section {section_id}"
                ) from exc
        except BaseException:
            try:
                Path(audio_path).unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove partial paragraph audio", extra={"run_id": run_id}, exc_info=True)
            raise
        finally:
            if entry.requires_gpu:
                gpu_lock.release(lock_id=lock_id)

        audio_seconds = _get_wav_duration_seconds(audio_path)
        try:
            await record_provider_call(
                run_id,
                entry.provider_type,
                tts_model,
                "tts",
                audio_seconds=audio_seconds,
                cost_usd=(
                    Decimal(str(audio_seconds)) * Decimal("0.0003")
                    if audio_seconds is not None else delivery_cost
                ),
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

        uploaded = store_artifact_file(run_id, audio_path, "audio/wav")
        artifact = await _audio_service.create_paragraph_artifact(
            run_id=run_id,
            section_id=section_id,
            path=audio_path,
            model_used=tts_model,
            provider_type=entry.provider_type,
            voice=voice,
            storage_provider=uploaded.storage_provider,
            storage_key=uploaded.key,
            idempotency_key=ctx.task_id,
        )

        return TaskResult(
            status="success",
            extra={
                "section_id": section_id,
                "tts_model": tts_model,
                "voice": voice,
                "provider_type": entry.provider_type,
                "endpoint": entry.endpoint,
                "gpu_lock_acquired_at": gpu_lock.acquired_at,
                "gpu_lock_released_at": gpu_lock.released_at,
                "audio_artifact_id": artifact.id,
                "audio_path": artifact.path,
            },
        )

    return run_task(self, run_id, config, execute)
