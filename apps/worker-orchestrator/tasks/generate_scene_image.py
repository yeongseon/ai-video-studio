# pyright: reportMissingImports=false
# ruff: noqa: E402

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from celery.exceptions import Ignore, SoftTimeLimitExceeded
from celery.utils.nodenames import gethostname
from celery_app import celery_app
from creator_domain.models.stage import RunStage
from creator_domain.sanitize import sanitize_path_component
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from creator_provider.registry import get_default_registry
from creator_service.cost_config import COST_SCENE_IMAGE
from creator_service.run_service import run_service as _run_service
from creator_service.telemetry import trace_task
from creator_service.usage_service import authorize_owned_provider_attempt
from creator_service.usage_service import record_provider_call
from creator_service.visual_asset_service import visual_asset_service as _visual_asset_service
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected
from creator_service.visual_plan_service import visual_plan_service as _visual_plan_service
from tasks.task_runner import GpuLockContext, TaskContext, TaskResult, TaskRunnerConfig, run_task
from tasks.hard_timeout import (
    delivery_owner,
    production_task,
    production_time_limits,
)
from tasks.task_execution import TaskInputError
from tasks.scene_batch_lifecycle import SAFE_FAILURE_STAGES, SAFE_SUCCESS_STAGES, SceneBatch

logger = logging.getLogger(__name__)
_ARTIFACTS_BASE = os.getenv("ARTIFACT_ROOT", "data/artifacts")
_SAFE_SUCCESS_STAGES = SAFE_SUCCESS_STAGES
_SAFE_FAILURE_STAGES = SAFE_FAILURE_STAGES
_SOFT_TIME_LIMIT, _HARD_TIME_LIMIT = production_time_limits(600, 660)

def _asset_dir(run_id: int) -> Path:
    return Path(_ARTIFACTS_BASE) / str(run_id) / "scenes"


def _cleanup_hard_timeout_outputs(
    run_id: int,
    task_id: str,
    worker_hostname: str,
    worker_pid: int,
) -> None:
    from worker_loop import run_in_worker_loop

    async def persisted_paths() -> set[Path]:
        grouped = await _visual_asset_service.list_by_run(run_id)
        return {
            Path(asset.asset_path).resolve()
            for assets in grouped.values()
            for asset in assets
        }

    persisted = run_in_worker_loop(persisted_paths())
    owner = delivery_owner(task_id, worker_hostname, worker_pid)
    for path in _asset_dir(run_id).glob(f"*-{owner}-*.png"):
        if path.resolve() not in persisted:
            path.unlink(missing_ok=True)

@celery_app.task(
    bind=True,
    base=production_task(_SAFE_FAILURE_STAGES, _cleanup_hard_timeout_outputs),
    autoretry_for=(ProviderTimeoutError, RateLimitError),
    retry_backoff=30,
    retry_jitter=True,
    max_retries=5,
    soft_time_limit=_SOFT_TIME_LIMIT,
    time_limit=_HARD_TIME_LIMIT,
    name="generate_scene_image",
)
@trace_task("generate_scene_image")
def generate_scene_image(
    self,
    run_id: int,
    scene_id: str | None = None,
    model_key: str = "sd15",
    prompt_override: str | None = None,
    is_active: bool = True,
    image_params: dict[str, Any] | None = None,
) -> dict[str, object]:
    config = TaskRunnerConfig(
        task_name="generate_scene_image",
        allowed_stages=frozenset(
            {
                RunStage.VISUAL_PLAN_REVIEW,
                RunStage.VISUAL_ASSET_GENERATING,
                RunStage.VISUAL_ASSET_REVIEW,
            }
        ),
        safe_stages=_SAFE_SUCCESS_STAGES,
        safe_failure_stages=_SAFE_FAILURE_STAGES,
        success_stage=None,
        requires_free_space=True,
    )

    async def execute_batch(ctx: TaskContext, batch: SceneBatch) -> TaskResult:
        plan = await _visual_plan_service.get_active_plan(run_id)
        if plan is None:
            raise TaskInputError(f"No active visual plan for run {run_id}")

        if scene_id is not None:
            target_scenes = [s for s in plan.scenes if s.scene_id == scene_id]
            if not target_scenes:
                raise TaskInputError(f"Scene '{scene_id}' not found in visual plan for run {run_id}")
        else:
            target_scenes = list(plan.scenes)
        if not target_scenes:
            raise TaskInputError(f"Visual plan for run {run_id} has no scenes")

        registry = get_default_registry()
        entry = registry.resolve(model_key)
        provider = registry.get_provider(model_key)
        asset_dir = _asset_dir(run_id)
        asset_dir.mkdir(parents=True, exist_ok=True)
        owner = delivery_owner(ctx.task_id, gethostname(), os.getpid())

        for idx, target_scene in enumerate(target_scenes):
            try:
                await batch.checkpoint()
                if idx > 0 and model_key in ("groq-svg", "pollinations", "hf-flux-schnell"):
                    await batch.backoff(16)
            except Ignore:
                batch.cancelled = True
                break
            scene_result: dict[str, object] = {
                "scene_id": target_scene.scene_id,
                "status": "pending",
            }
            effective_prompt = (
                prompt_override
                if prompt_override is not None and scene_id is not None
                else target_scene.prompt
            )
            # Apply quality profile image style prefix for photo-quality providers
            if model_key in ("pollinations", "hf-flux-schnell"):
                try:
                    from creator_service.recipe_profile import resolve_quality_profile
                    qp = resolve_quality_profile("shorts_default")
                    if qp.image_style_prefix and not effective_prompt.startswith(qp.image_style_prefix):
                        effective_prompt = qp.image_style_prefix + effective_prompt
                except Exception:
                    pass  # Non-critical enhancement
            gpu_lock = GpuLockContext(f"{ctx.task_id}:{target_scene.scene_id}")
            provider_error: ProviderError | None = None
            usage_error: Exception | None = None

            try:
                if entry.requires_gpu:
                    gpu_lock.acquire()
                try:
                    params = dict(entry.default_params or {})
                    if image_params:
                        params.update(image_params)
                    safe_scene_id = sanitize_path_component(target_scene.scene_id, label="scene_id")
                    target_path = str(
                        asset_dir / f"{safe_scene_id}-{owner}-{uuid4().hex}.png"
                    )
                    batch.local_outputs.add(Path(target_path))
                    Path(target_path).touch()
                    params["output_path"] = target_path
                    # Per-scene retry for SVG/XML parse errors (up to 3 attempts for groq-svg)
                    _scene_max_attempts = 3 if model_key == "groq-svg" else 1
                    for _scene_attempt in range(_scene_max_attempts):
                        await batch.checkpoint()
                        try:
                            attempt_id = (
                                f"{ctx.task_id}:{ctx.delivery_attempt}:"
                                f"{target_scene.scene_id}:{_scene_attempt}"
                            )
                            try:
                                if not await authorize_owned_provider_attempt(
                                    ctx.reservation_owner_id, attempt_id, COST_SCENE_IMAGE,
                                ):
                                    raise ProviderError("Provider attempt exceeds approved spend")
                                ctx.mark_provider_started()
                                await provider.generate(effective_prompt, params)
                            finally:
                                await batch.checkpoint()
                            break  # Success — exit retry loop
                        except Ignore:
                            raise
                        except ProviderError as exc:
                            provider_error = exc
                            raise
                        except (TimeoutError, ConnectionError) as exc:
                            raise ProviderTimeoutError(
                                "Provider timed out during scene image generation "
                                f"for run {run_id} scene {target_scene.scene_id}"
                            ) from exc
                        except SoftTimeLimitExceeded:
                            raise
                        except Exception as exc:
                            logger.error(
                                "Image generation exception for run %d scene %s (attempt %d/%d): %s: %s",
                                run_id, target_scene.scene_id,
                                _scene_attempt + 1, _scene_max_attempts,
                                type(exc).__name__, exc,
                                exc_info=True,
                            )
                            message = str(exc).lower()
                            if "429" in message or "rate limit" in message or "too many requests" in message:
                                raise RateLimitError(
                                    "Provider rate limited scene image generation "
                                    f"for run {run_id} scene {target_scene.scene_id}"
                                ) from exc
                            # Retry on parse/SVG/XML errors
                            is_parse_error = any(
                                kw in message for kw in ("parse", "xml", "svg", "valid")
                            )
                            if _scene_attempt < _scene_max_attempts - 1 and is_parse_error:
                                logger.warning(
                                    "Scene %s SVG error on attempt %d, retrying in 5s...",
                                    target_scene.scene_id, _scene_attempt + 1,
                                )
                                await batch.backoff(5)
                                # Regenerate output path for retry
                                target_path = str(
                                    asset_dir
                                    / f"{safe_scene_id}-{owner}-{uuid4().hex}.png"
                                )
                                params["output_path"] = target_path
                                batch.local_outputs.add(Path(target_path))
                                Path(target_path).touch()
                                continue
                            raise ProviderError(
                                "Provider failed scene image generation "
                                f"for run {run_id} scene {target_scene.scene_id}"
                            ) from exc
                finally:
                    if entry.requires_gpu:
                        gpu_lock.release()

                uploaded = await batch.upload(target_path)
                try:
                    asset = await _visual_asset_service.create_asset(
                        run_id=run_id,
                        scene_id=target_scene.scene_id,
                        asset_path=target_path,
                        prompt_snapshot=effective_prompt,
                        model_used=model_key,
                        provider_type=entry.provider_type,
                        storage_provider=uploaded.storage_provider,
                        storage_key=uploaded.key,
                        is_active=is_active,
                        idempotency_key=f"{ctx.task_id}:{target_scene.scene_id}",
                    )
                except SceneAssetAcceptanceRejected:
                    await batch.discard_rejected_upload(target_path, uploaded)
                    raise Ignore() from None

                scene_result.update(
                    {
                        "status": "success",
                        "asset_id": asset.id,
                        "asset_path": asset.asset_path,
                        "version": asset.version,
                        "gpu_lock_acquired_at": gpu_lock.acquired_at,
                        "gpu_lock_released_at": gpu_lock.released_at,
                    }
                )
                batch.results.append(scene_result)
                await batch.checkpoint()

                try:
                    await record_provider_call(
                        run_id,
                        entry.provider_type,
                        model_key,
                        "image_gen",
                        cost_usd=COST_SCENE_IMAGE,
                        workspace_id=ctx.workspace_id,
                        project_id=ctx.project_id,
                        idempotency_key=f"{ctx.task_id}:{target_scene.scene_id}",
                        reservation_owner_id=ctx.reservation_owner_id,
                        reservation_attempt_id=attempt_id,
                    )
                    ctx.mark_provider_accounted()
                    await batch.checkpoint()
                except Ignore:
                    await _visual_asset_service.storage.delete_asset(asset.id)
                    await batch.discard_rejected_upload(target_path, uploaded)
                    batch.results.remove(scene_result)
                    raise
                except Exception as exc:
                    if ctx.reservation_owner_id is not None:
                        usage_error = exc
                        raise
                    logger.warning("Failed to record provider usage", exc_info=True)

                await batch.checkpoint()
            except Ignore:
                batch.cancelled = True
                break
            except SoftTimeLimitExceeded:
                raise
            except ProviderTimeoutError:
                raise
            except RateLimitError:
                raise
            except Exception as exc:
                if exc is provider_error or exc is usage_error:
                    raise
                scene_result.update({"status": "failed", "error": str(exc)})
                batch.failures.append(scene_result)
                logger.error(
                    "Failed to generate image for scene %s in run %d: %s",
                    target_scene.scene_id,
                    run_id,
                    exc,
                )

        return await batch.finish(
            len(target_scenes),
            {
                "model_key": model_key,
                "provider_type": entry.provider_type,
                "endpoint": entry.endpoint,
                "scene_id": scene_id,
            },
        )

    async def execute(ctx: TaskContext) -> TaskResult:
        batch = SceneBatch(ctx, _run_service)
        try:
            return await execute_batch(ctx, batch)
        finally:
            batch.cleanup_local_outputs()

    return run_task(self, run_id, config, execute)
