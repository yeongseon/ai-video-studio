from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from typing import Protocol

from billiard.exceptions import SoftTimeLimitExceeded

from creator_service.object_storage import ArtifactStorageBackend
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)

logger = logging.getLogger(__name__)


class RetryConnection(Protocol):
    async def fetchrow(self, query: str, *args: object): ...
    async def fetchval(self, query: str, *args: object): ...
    async def execute(self, query: str, *args: object): ...


async def retry_failed_deletions(
    max_retries: int,
    transaction_factory: Callable,
    storage_backend_factory: Callable[[], ArtifactStorageBackend],
) -> int:
    """Delete retry candidates while their database row locks remain held."""
    attempted_ids: list[int] = []
    deleted_count = 0
    failed_count = 0

    for _ in range(500):
        async with transaction_factory() as conn:
            row = await conn.fetchrow(
                "SELECT id, run_id, storage_key, storage_provider, file_path, "
                "metadata_json, delete_retry_count FROM creator_artifacts "
                "WHERE (delete_failed_at IS NOT NULL OR "
                "(delete_requested_at IS NOT NULL AND delete_failed_at IS NULL)) "
                "AND COALESCE(delete_retry_count, 0) < $1 "
                "AND storage_key IS NOT NULL AND storage_key != '' "
                "AND (metadata_json != $3 OR created_at < NOW() - INTERVAL '5 minutes') "
                "AND NOT (id = ANY($2::int[])) "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                max_retries,
                attempted_ids,
                SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            )
            if row is None:
                break
            artifact = dict(row)
            key = artifact["storage_key"]
            artifact_id = artifact["id"]
            attempted_ids.append(artifact_id)
            try:
                accepted_scene_id = None
                if artifact.get("metadata_json") == SCENE_UPLOAD_CLEANUP_INTENT_MARKER:
                    accepted_scene_id = await conn.fetchval(
                        "SELECT id FROM creator_scene_assets "
                        "WHERE run_id = $1 AND storage_key = $2 AND is_active = true "
                        "AND (storage_provider = $3 OR storage_provider IS NULL OR $3 IS NULL) "
                        "LIMIT 1",
                        artifact["run_id"],
                        key,
                        artifact.get("storage_provider"),
                    )
                if accepted_scene_id is not None:
                    await conn.execute("DELETE FROM creator_artifacts WHERE id = $1", artifact_id)
                    deleted_count += 1
                    continue
                storage_backend_factory().delete(key)
                if artifact.get("metadata_json") in {
                    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
                    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                }:
                    local_path = artifact.get("file_path")
                    if not isinstance(local_path, str):
                        raise OSError("rejected upload cleanup path missing")
                    from pathlib import Path

                    Path(local_path).unlink(missing_ok=True)
                await conn.execute("DELETE FROM creator_artifacts WHERE id = $1", artifact_id)
                deleted_count += 1
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                fingerprint = hashlib.sha256(key.encode()).hexdigest()[:16]
                logger.error(
                    "Artifact deletion retry failed",
                    extra={
                        "artifact_id": artifact_id,
                        "storage_provider": "configured",
                        "key_fingerprint": fingerprint,
                        "error_type": type(exc).__name__,
                    },
                )
                await conn.execute(
                    "UPDATE creator_artifacts SET delete_failed_at = NOW(), delete_error = $2, "
                    "delete_retry_count = COALESCE(delete_retry_count, 0) + 1 WHERE id = $1",
                    artifact_id,
                    f"provider=configured; fingerprint={fingerprint}",
                )
                failed_count += 1

    async with transaction_factory() as conn:
        abandoned_count = await conn.fetchval(
            "SELECT COUNT(*) FROM creator_artifacts "
            "WHERE COALESCE(delete_retry_count, 0) >= $1",
            max_retries,
        )
        if abandoned_count and abandoned_count > 0:
            logger.warning(
                "retry_failed_deletions: %d artifacts have exhausted max_retries=%d "
                "and will not be retried — storage orphans may need manual cleanup",
                abandoned_count,
                max_retries,
            )
    if failed_count:
        logger.warning(
            "retry_failed_deletions: %d succeeded, %d failed",
            deleted_count,
            failed_count,
        )
    return deleted_count
