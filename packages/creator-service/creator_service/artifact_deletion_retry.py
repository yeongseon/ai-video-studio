from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict

from billiard.exceptions import SoftTimeLimitExceeded

from creator_service.confined_directory_deletion import delete_artifact_file
from creator_service.object_storage import ArtifactStorageBackend
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)
from creator_service.run_object_deletion import delete_run_object

logger = logging.getLogger(__name__)


class RetryConnection(Protocol):
    async def fetchrow(self, query: str, *args: object) -> RetryArtifact | None: ...
    async def fetchval(self, query: str, *args: object): ...
    async def execute(self, query: str, *args: object): ...


class RetryArtifact(TypedDict):
    id: int
    run_id: int
    storage_key: str
    storage_provider: str | None
    file_path: str | None
    metadata_json: str | None
    delete_failed_at: datetime | None


def _row_removable_after_success(artifact: RetryArtifact) -> bool:
    # late-write residual tracked in #956; retaining a tombstone is a one-line change here
    return True


async def _has_accepted_reference(conn: RetryConnection, artifact: RetryArtifact) -> bool:
    if artifact.get("metadata_json") != SCENE_UPLOAD_CLEANUP_INTENT_MARKER:
        return False
    return await conn.fetchval(
        "SELECT id FROM creator_scene_assets "
        "WHERE run_id = $1 AND storage_key = $2 "
        "AND (storage_provider = $3 OR storage_provider IS NULL OR $3 IS NULL) "
        "LIMIT 1",
        artifact["run_id"], artifact["storage_key"], artifact.get("storage_provider"),
    ) is not None


async def _delete_locked_candidate(
    conn: RetryConnection,
    artifact: RetryArtifact,
    storage_backend_factory: Callable[[], ArtifactStorageBackend],
) -> bool:
    key = artifact["storage_key"]
    artifact_id = artifact["id"]
    try:
        if await _has_accepted_reference(conn, artifact):
            await conn.execute("DELETE FROM creator_artifacts WHERE id = $1", artifact_id)
            return True
        delete_run_object(key, artifact.get("storage_provider"), storage_backend_factory())
        if artifact.get("metadata_json") in {
            REJECTED_SCENE_UPLOAD_CLEANUP_MARKER, SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
        }:
            local_path = artifact.get("file_path")
            if not isinstance(local_path, str):
                raise OSError("rejected upload cleanup path missing")
            delete_artifact_file(Path(local_path))
        if _row_removable_after_success(artifact):
            await conn.execute("DELETE FROM creator_artifacts WHERE id = $1", artifact_id)
        return True
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
        return False


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
                "metadata_json, delete_retry_count, delete_failed_at FROM creator_artifacts "
                "WHERE (delete_failed_at IS NOT NULL OR "
                "(delete_requested_at IS NOT NULL AND delete_failed_at IS NULL)) "
                "AND COALESCE(delete_retry_count, 0) < $1 "
                "AND storage_key IS NOT NULL AND storage_key != '' "
                "AND (metadata_json IS DISTINCT FROM $3 OR delete_failed_at IS NOT NULL "
                "OR created_at < NOW() - INTERVAL '5 minutes') "
                "AND NOT (id = ANY($2::int[])) "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                max_retries,
                attempted_ids,
                SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            )
            if row is None:
                break
            artifact = row
            artifact_id = artifact["id"]
            attempted_ids.append(artifact_id)
            is_intent = artifact.get("metadata_json") == SCENE_UPLOAD_CLEANUP_INTENT_MARKER
            if is_intent:
                if await _has_accepted_reference(conn, artifact):
                    await conn.execute("DELETE FROM creator_artifacts WHERE id = $1", artifact_id)
                    deleted_count += 1
                    continue
                if artifact.get("delete_failed_at") is None:
                    await conn.execute(
                        "UPDATE creator_artifacts SET delete_failed_at = clock_timestamp(), "
                        "delete_error = 'scene_upload_reclaim_started' "
                        "WHERE id = $1 AND metadata_json = $2 AND delete_failed_at IS NULL",
                        artifact_id, SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                    )
            else:
                succeeded = await _delete_locked_candidate(conn, artifact, storage_backend_factory)
                deleted_count += int(succeeded)
                failed_count += int(not succeeded)
        if is_intent:
            async with transaction_factory() as conn:
                row = await conn.fetchrow(
                    "SELECT id, run_id, storage_key, storage_provider, file_path, "
                    "metadata_json, delete_retry_count, delete_failed_at FROM creator_artifacts "
                    "WHERE id = $1 FOR UPDATE SKIP LOCKED", artifact_id,
                )
                if row is None:
                    continue
                artifact = row
                succeeded = await _delete_locked_candidate(conn, artifact, storage_backend_factory)
                deleted_count += int(succeeded)
                failed_count += int(not succeeded)

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
