"""Admin API endpoints service operations for run management, system health, cache clearing, queue depth, storage stats, and auth/rate-limit infrastructure."""

from __future__ import annotations

import inspect
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import redis.asyncio as redis
from celery import current_app as celery_app  # noqa: F401
from creator_service.admin_task_broker import CeleryTaskBroker as CeleryTaskBroker, TaskBroker as TaskBroker

from creator_service.db import get_pool
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)

_GENERATING_TO_REVIEW_STAGE = {
    "SCRIPT_GENERATING": "SCRIPT_REVIEW",
    "VISUAL_PLAN_GENERATING": "VISUAL_PLAN_REVIEW",
    "VISUAL_ASSET_GENERATING": "VISUAL_ASSET_REVIEW",
    "AUDIO_GENERATING": "SUBTITLE_GENERATING",
    "SUBTITLE_GENERATING": "RENDER_GENERATING",
    "RENDER_GENERATING": "FINAL_REVIEW",
}

_UNSTICK_MIN_AGE_SECONDS = 30 * 60
_STAGE_REQUIRED_ARTIFACTS: dict[str, list[str]] = {
    "SCRIPT_GENERATING": [],
    "SCRIPT_REVIEW": ["script"],
    "VOICE_GENERATING": ["script"],
    "VOICE_REVIEW": ["script", "voice"],
    "VIDEO_GENERATING": ["script", "voice"],
    "VIDEO_REVIEW": ["script", "voice", "video"],
    "COMPLETED": ["script", "voice", "video"],
    "VISUAL_PLAN_REVIEW": ["script"],
    "VISUAL_ASSET_REVIEW": ["script", "visual_plan"],
    "SUBTITLE_GENERATING": ["script", "audio"],
    "RENDER_GENERATING": ["script", "audio", "subtitle"],
    "FINAL_REVIEW": ["script", "audio", "subtitle", "render"],
    "PUBLISHED": ["script", "audio", "subtitle", "render"],
}
logger = logging.getLogger(__name__)


class AdminService:
    def __init__(self, task_broker: TaskBroker | None = None) -> None:
        self._started_at = time.time()
        self._task_broker = task_broker or CeleryTaskBroker()

    def _redis_client(self) -> redis.Redis:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
        return redis.from_url(redis_url, decode_responses=True)

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _get_run_artifact_types(self, run_id: int, connection: Any) -> set[str]:
        rows = await connection.fetch(
            "SELECT DISTINCT artifact_type FROM creator_artifacts WHERE run_id = $1 "
            "AND metadata_json IS DISTINCT FROM $2 AND metadata_json IS DISTINCT FROM $3",
            run_id,
            REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
            SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
        )
        return {str(row["artifact_type"]) for row in rows if row.get("artifact_type")}

    async def get_system_health(self) -> dict[str, Any]:
        uptime_seconds = int(time.time() - self._started_at)
        health: dict[str, Any] = {
            "status": "ok",
            "db": {"ok": True},
            "redis": {"ok": True},
            "uptime_seconds": uptime_seconds,
        }

        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
        except Exception:
            health["status"] = "degraded"
            health["db"] = {"ok": False, "error": "UNAVAILABLE"}

        client: redis.Redis | None = None
        try:
            client = self._redis_client()
            await self._maybe_await(client.ping())
        except Exception:
            health["status"] = "degraded"
            health["redis"] = {"ok": False, "error": "UNAVAILABLE"}
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    health["status"] = "degraded"
                    health["redis"] = {"ok": False, "error": "UNAVAILABLE"}

        return health

    async def get_stuck_runs(self, threshold_minutes: int = 30) -> list[dict[str, Any]]:
        threshold_minutes = max(1, threshold_minutes)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=threshold_minutes)
        try:
            pool = await get_pool()
            stages = list(_GENERATING_TO_REVIEW_STAGE.keys())
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT id, project_id, current_stage, status, updated_at
                    FROM creator_runs
                    WHERE current_stage = ANY($1::text[])
                      AND updated_at < $2
                    ORDER BY updated_at ASC
                    """,
                    stages,
                    cutoff,
                )
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to query stuck runs")
            return []

    async def get_failed_runs(self, hours: int = 24) -> list[dict[str, Any]]:
        hours = max(1, hours)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    """
                    SELECT id, project_id, current_stage, status, updated_at
                    FROM creator_runs
                    WHERE status = 'failed'
                      AND updated_at >= $1
                    ORDER BY updated_at DESC
                    """,
                    cutoff,
                )
            return [dict(row) for row in rows]
        except Exception:
            logger.exception("Failed to query failed runs")
            return []

    async def get_queue_depth(self) -> dict[str, int]:
        queue_names = ["creator"]
        result: dict[str, int] = {name: 0 for name in queue_names}
        client: redis.Redis | None = None
        try:
            client = self._redis_client()
            for queue_name in queue_names:
                depth = await self._maybe_await(client.llen(queue_name))
                result[queue_name] = int(depth)
        except Exception:
            return result
        finally:
            if client is not None:
                await client.aclose()
        return result

    async def get_storage_stats(self) -> dict[str, Any]:
        artifact_root = Path(os.getenv("ARTIFACT_ROOT", "./data/artifacts"))
        total_files = 0
        total_size_bytes = 0
        try:
            if artifact_root.exists():
                for file_path in artifact_root.rglob("*"):
                    if file_path.is_file():
                        total_files += 1
                        total_size_bytes += file_path.stat().st_size
            return {
                "artifact_root": str(artifact_root),
                "file_count": total_files,
                "total_size_bytes": total_size_bytes,
            }
        except Exception as exc:
            return {
                "artifact_root": str(artifact_root),
                "file_count": total_files,
                "total_size_bytes": total_size_bytes,
                "error": str(exc),
            }

    async def unstick_run(self, run_id: str) -> dict[str, Any]:
        try:
            run_id_int = int(run_id)
        except ValueError:
            return {"ok": False, "run_id": run_id, "error": "Invalid run_id"}

        try:
            pool = await get_pool()
            async with pool.acquire() as connection:
                row = await connection.fetchrow(
                    "SELECT id, current_stage, status, updated_at FROM creator_runs WHERE id = $1",
                    run_id_int,
                )
                if row is None:
                    return {"ok": False, "run_id": run_id, "error": "Run not found"}

                current_stage = row["current_stage"]
                target_stage = _GENERATING_TO_REVIEW_STAGE.get(current_stage)
                if target_stage is None:
                    return {
                        "ok": False,
                        "run_id": run_id,
                        "current_stage": current_stage,
                        "error": "Run is not in a generating stage",
                    }

                run_status = row["status"]
                if run_status != "running":
                    return {
                        "ok": False,
                        "run_id": run_id,
                        "current_stage": current_stage,
                        "status": run_status,
                        "error": "Run is not in 'running' status",
                    }

                updated_at = row["updated_at"]
                if not isinstance(updated_at, datetime):
                    return {
                        "ok": False,
                        "run_id": run_id,
                        "current_stage": current_stage,
                        "error": "Run missing stage timestamp",
                    }
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)

                stage_age_seconds = (datetime.now(tz=timezone.utc) - updated_at).total_seconds()
                if stage_age_seconds <= _UNSTICK_MIN_AGE_SECONDS:
                    return {
                        "ok": False,
                        "run_id": run_id,
                        "current_stage": current_stage,
                        "error": "Run has not been stuck long enough",
                    }

                required_artifacts = _STAGE_REQUIRED_ARTIFACTS.get(target_stage, [])
                if required_artifacts:
                    existing_artifacts = await self._get_run_artifact_types(run_id_int, connection)
                    missing_artifacts = [
                        artifact_type
                        for artifact_type in required_artifacts
                        if artifact_type not in existing_artifacts
                    ]
                    if missing_artifacts:
                        return {
                            "ok": False,
                            "run_id": run_id,
                            "current_stage": current_stage,
                            "error": (
                                f"Cannot advance to {target_stage}: missing required artifacts: "
                                f"{missing_artifacts}"
                            ),
                        }

                updated = await connection.fetchrow(
                    """
                    UPDATE creator_runs
                    SET current_stage = $2,
                        status = 'pending'
                    WHERE id = $1
                      AND current_stage = $3
                      AND status = $4
                      AND updated_at = $5
                    RETURNING id, current_stage, status, updated_at
                    """,
                    run_id_int,
                    target_stage,
                    current_stage,
                    run_status,
                    row["updated_at"],
                )
                if updated is None:
                    return {
                        "ok": False,
                        "run_id": run_id,
                        "error": "Run changed concurrently; retry unstick",
                    }

                revoke_warnings: list[str] = []
                from creator_service.task_tracking_service import task_tracking_service

                active_celery_ids = await task_tracking_service.get_active_celery_ids(run_id_int)
                for task_id in active_celery_ids:
                    try:
                        await self._task_broker.revoke_task(task_id)
                        await task_tracking_service.mark_revoked(task_id)
                    except Exception as exc:
                        logger.warning(
                            "Failed to revoke task %s for run %s: %s", task_id, run_id, exc
                        )
                        revoke_warnings.append(f"Failed to revoke task {task_id}: {exc}")

                logger.warning(
                    "Admin mutation executed: unstick run_id=%s from=%s to=%s age_seconds=%.1f",
                    run_id_int,
                    current_stage,
                    target_stage,
                    stage_age_seconds,
                )

                return {
                    "ok": True,
                    "run_id": str(updated["id"]),
                    "previous_stage": current_stage,
                    "current_stage": updated["current_stage"],
                    "status": updated["status"],
                    "warnings": revoke_warnings or None,
                }
        except Exception:
            logger.exception("Unstick run failed for run_id=%s", run_id)
            return {
                "ok": False,
                "run_id": run_id,
                "error": "Internal error during unstick operation",
            }


    # Unsafe glob metacharacters that could cause Redis SCAN abuse
    _UNSAFE_PATTERN_CHARS = frozenset("[?\\")

    @staticmethod
    def _validate_cache_pattern(pattern: str) -> str | None:
        """Validate a cache clear pattern. Returns error message or None if valid."""
        # Reject newline/carriage-return (defense-in-depth for log injection)
        if "\n" in pattern or "\r" in pattern:
            return "Pattern contains unsafe newline characters"

        # Must start with cache: prefix
        if not pattern.startswith("cache:"):
            return "Pattern must start with 'cache:' to prevent accidental data wipe"

        # Reject unsafe glob metacharacters
        for char in AdminService._UNSAFE_PATTERN_CHARS:
            if char in pattern:
                return f"Pattern contains unsafe character '{char}'"

        # Must have a sub-namespace (cache:<sub>:...) — bare cache:* is too broad
        after_prefix = pattern[len("cache:"):]
        colon_pos = after_prefix.find(":")
        if colon_pos < 1:
            # No sub-namespace found (e.g., "cache:*", "cache:", "cache:x")
            if "*" in after_prefix or not after_prefix:
                return (
                    "Pattern 'cache:*' is too broad; specify a sub-namespace "
                    "like 'cache:model:*'"
                )
            # "cache:something" without colon — reject as ambiguous
            return (
                "Pattern must include a sub-namespace separator "
                "(e.g., 'cache:model:*' not 'cache:model')"
            )

        # Sub-namespace segment must be alphanumeric (no wildcards in namespace)
        sub_namespace = after_prefix[:colon_pos]
        if not sub_namespace.replace("_", "").replace("-", "").isalnum():
            return (
                f"Sub-namespace '{sub_namespace}' contains invalid characters; "
                "only alphanumeric, hyphens, and underscores are allowed"
            )

        return None

    async def clear_cache(
        self, key_pattern: str | None = None, dry_run: bool = False
    ) -> dict[str, Any]:
        protected_keys = {"celery", "gpu_queue"}
        deleted = 0
        resolved_pattern = key_pattern or os.getenv("ADMIN_CACHE_CLEAR_PREFIX", "cache:app:*")
        # Validate pattern safety
        pattern_error = self._validate_cache_pattern(resolved_pattern)
        if pattern_error is not None:
            return {
                "ok": False,
                "deleted_keys": 0,
                "key_pattern": resolved_pattern,
                "dry_run": dry_run,
                "matched_keys": [],
                "error": pattern_error,
            }
        matched_keys: list[str] = []
        client: redis.Redis | None = None
        try:
            client = self._redis_client()
            keys = [key async for key in client.scan_iter(match=resolved_pattern)]
            for key in keys:
                if key in protected_keys:
                    continue
                matched_keys.append(key)
                if dry_run:
                    continue
                deleted_result = client.delete(key)
                if inspect.isawaitable(deleted_result):
                    deleted += int(await deleted_result)
                else:
                    deleted += int(deleted_result)
            logger.warning(
                "Admin mutation executed: clear cache key_pattern=%s dry_run=%s matched=%d deleted=%d",
                resolved_pattern,
                dry_run,
                len(matched_keys),
                deleted,
            )
            return {
                "ok": True,
                "deleted_keys": deleted,
                "key_pattern": resolved_pattern,
                "dry_run": dry_run,
                "matched_keys": matched_keys,
            }
        except Exception:
            logger.exception(
                "Clear cache failed for pattern=%s dry_run=%s", resolved_pattern, dry_run
            )
            return {
                "ok": False,
                "deleted_keys": deleted,
                "key_pattern": resolved_pattern,
                "dry_run": dry_run,
                "matched_keys": matched_keys,
                "error": "Internal error during cache operation",
            }
        finally:
            if client is not None:
                await client.aclose()


admin_service = AdminService()
