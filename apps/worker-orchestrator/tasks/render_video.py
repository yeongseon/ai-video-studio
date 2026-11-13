from __future__ import annotations

import logging
import os
from pathlib import Path

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from celery_app import celery_app
from creator_domain.models.stage import RunStage
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from creator_service.audio_service import audio_service as _audio_service
from creator_service.cost_config import COST_RENDER_VIDEO
from creator_service.ffmpeg_service import FFmpegService
from creator_service.media_integrity import MediaIntegrityError
from creator_service.render_segments import render_input_from_plan
from creator_service.render_service import render_service as _render_service
from creator_service.script_service import script_service as _script_service
from creator_service.subtitle_service import subtitle_service as _subtitle_service
from creator_service.telemetry import trace_task
from creator_service.usage_service import authorize_owned_provider_attempt, record_provider_call
from creator_service.visual_asset_service import visual_asset_service as _visual_asset_service
from creator_service.visual_plan_service import visual_plan_service as _visual_plan_service
from tasks.legacy_render_adapter import prepare_legacy_render
from tasks.legacy_render_audio import compute_scene_durations as compute_scene_durations
from tasks.legacy_render_contract import (
    LegacyRequest,
    LegacyServices,
    build_render_plan as _build_render_plan,
    resolve_profile as _resolve_profile,
)
from tasks.task_runner import TaskContext, TaskResult, TaskRunnerConfig, run_task
from tasks.hard_timeout import production_task
from tasks.timeline_render import RenderLayers, read_render_metadata, render_saved_timeline

logger = logging.getLogger(__name__)
_ARTIFACT_ROOT = os.getenv("ARTIFACT_ROOT", "data/artifacts")


async def execute_render(ctx: TaskContext, render_profile: str = "shorts_default") -> TaskResult:
    """Shared task body for integrated tests; stage transitions belong to run_task."""
    request = LegacyRequest(ctx.run_id, render_profile, _ARTIFACT_ROOT)
    output = request.output("output.mp4")
    audio: Path | None = None
    subtitles: Path | None = None
    attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}:render"
    if read_render_metadata(ctx).render_source == "timeline":
        narration = await _audio_service.get_latest(ctx.run_id)
        captions = await _subtitle_service.get_latest(ctx.run_id)
        audio = request.path(narration.path) if narration else None
        subtitles = request.path(captions.path) if captions else None
        if not await authorize_owned_provider_attempt(
            ctx.reservation_owner_id, attempt_id, COST_RENDER_VIDEO,
        ):
            raise ProviderError("Provider attempt exceeds approved spend")
        ctx.mark_provider_started()
        try:
            scene_count = await render_saved_timeline(
                ctx, _resolve_profile(render_profile), output, layers=RenderLayers(audio, subtitles),
            )
        except MediaIntegrityError as exc:
            raise ProviderError(f"Provider failed video render for run {ctx.run_id}") from exc
    else:
        inputs = await prepare_legacy_render(request, LegacyServices(
            render=_render_service, visuals=_visual_asset_service, audio=_audio_service,
            subtitles=_subtitle_service, plans=_visual_plan_service, scripts=_script_service,
            ffmpeg_factory=FFmpegService,
        ))
        audio, subtitles, scene_count = inputs.audio, inputs.subtitles, inputs.scene_count
        render_input = render_input_from_plan(inputs.plan, audio_path=audio, subtitle_path=subtitles)
        try:
            if not await authorize_owned_provider_attempt(
                ctx.reservation_owner_id, attempt_id, COST_RENDER_VIDEO,
            ):
                raise ProviderError("Provider attempt exceeds approved spend")
            ctx.mark_provider_started()
            inputs.ffmpeg.render(render_input, output)
        except (TimeoutError, ConnectionError) as exc:
            raise ProviderTimeoutError(f"Provider timed out during video render for run {ctx.run_id}") from exc
        except SoftTimeLimitExceeded:
            raise
        except MediaIntegrityError as exc:
            raise ProviderError(f"Provider failed video render for run {ctx.run_id}") from exc
        except Exception as exc:
            logger.error("FFmpeg render exception for run %d: %s", ctx.run_id, type(exc).__name__)
            if "429" in str(exc).lower() or "rate limit" in str(exc).lower():
                raise RateLimitError(f"Provider rate limited video render for run {ctx.run_id}") from exc
            raise ProviderError(f"Provider failed video render for run {ctx.run_id}") from exc
    try:
        await record_provider_call(
            ctx.run_id, "ffmpeg", render_profile, "render", cost_usd=COST_RENDER_VIDEO,
            workspace_id=ctx.workspace_id, project_id=ctx.project_id, idempotency_key=ctx.task_id,
            reservation_owner_id=ctx.reservation_owner_id,
            reservation_attempt_id=attempt_id,
        )
        ctx.mark_provider_accounted()
    except Exception:
        if ctx.reservation_owner_id is not None:
            raise
        logger.warning("Failed to record provider usage", exc_info=True)
    from creator_service.artifact_storage_integration import store_artifact_file

    uploaded = store_artifact_file(ctx.run_id, output, "video/mp4")
    artifact = await _render_service.create_artifact(
        run_id=ctx.run_id, path=str(output), render_profile=render_profile,
        storage_provider=uploaded.storage_provider, storage_key=uploaded.key,
        idempotency_key=ctx.task_id,
    )
    return TaskResult(extra={
        "render_profile": render_profile, "video_artifact_id": artifact.id,
        "video_path": artifact.path, "scene_count": scene_count,
        "audio_path": str(audio) if audio else None,
        "subtitle_path": str(subtitles) if subtitles else None,
    })


@celery_app.task(
    bind=True, base=production_task(frozenset({RunStage.RENDER_GENERATING.value})),
    autoretry_for=(ProviderTimeoutError, RateLimitError),
    retry_backoff=True, retry_jitter=True, max_retries=3,
    soft_time_limit=600, time_limit=660, name="render_video",
)
@trace_task("render_video")
def render_video(self: Task, run_id: int, render_profile: str = "shorts_default") -> dict[str, object]:
    config = TaskRunnerConfig(
        task_name="render_video", allowed_stages=frozenset({RunStage.RENDER_GENERATING}),
        safe_stages=frozenset({RunStage.RENDER_GENERATING.value}), success_stage=RunStage.FINAL_REVIEW.value,
        requires_free_space=True,
    )

    async def execute(ctx: TaskContext) -> TaskResult:
        return await execute_render(ctx, render_profile)

    return run_task(self, run_id, config, execute)
