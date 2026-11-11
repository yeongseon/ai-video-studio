from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict

from billiard.exceptions import SoftTimeLimitExceeded

from creator_service.confined_directory_deletion import delete_artifact_file
from creator_service.media_upload_reconciliation import LATE_UPLOAD_WATCH_RETENTION
from creator_service.object_storage import ArtifactStorageBackend
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    SCENE_UPLOAD_LATE_WATCH_MARKER,
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
    delete_error: str | None


def _row_removable_after_success(artifact: RetryArtifact) -> bool:
    """Only a resolved upload may lose its row; an unresolved one first becomes a late-write watch.

    A reclaimed intent may belong to an upload that is still in flight. Keeping the fenced row
    for ``LATE_UPLOAD_WATCH_RETENTION`` (the media pipeline's policy) makes acceptance of that
    key fail and lets one last sweep delete bytes that land late. The row is erased only by
    that final sweep.
    """
    if artifact.get("metadata_json") != SCENE_UPLOAD_CLEANUP_INTENT_MARKER:
        return True
    return artifact.get("delete_error") == SCENE_UPLOAD_LATE_WATCH_MARKER


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
        else:
            await conn.execute(
                "UPDATE creator_artifacts SET delete_failed_at = clock_timestamp(), "
                "delete_error = $2 WHERE id = $1",
                artifact_id, SCENE_UPLOAD_LATE_WATCH_MARKER,
            )
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


_ARTIFACT_COLUMNS = (
    "id, run_id, storage_key, storage_provider, file_path, metadata_json, "
    "delete_retry_count, delete_failed_at, delete_error"
)
_WATCH_HORIZON_SECONDS = LATE_UPLOAD_WATCH_RETENTION.total_seconds()


def _watch_horizon_elapsed(horizon_param: int, marker_param: int) -> str:
    """SQL predicate shared by both reclaim transactions: a watch is a candidate once elapsed.

    The second transaction must re-check it, because another reclaimer can finish its own pass,
    and so create a fresh watch, between this reclaimer's two transactions. Only parameter
    positions are interpolated.
    """
    return (
        f"(delete_error IS DISTINCT FROM ${marker_param} "
        f"OR delete_failed_at < NOW() - make_interval(secs => ${horizon_param}))"
    )


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
                f"SELECT {_ARTIFACT_COLUMNS} FROM creator_artifacts "
                "WHERE (delete_failed_at IS NOT NULL OR "
                "(delete_requested_at IS NOT NULL AND delete_failed_at IS NULL)) "
                "AND COALESCE(delete_retry_count, 0) < $1 "
                "AND storage_key IS NOT NULL AND storage_key != '' "
                "AND (metadata_json IS DISTINCT FROM $3 OR delete_failed_at IS NOT NULL "
                "OR created_at < NOW() - INTERVAL '5 minutes') "
                f"AND {_watch_horizon_elapsed(4, 5)} "
                "AND NOT (id = ANY($2::int[])) "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
                max_retries,
                attempted_ids,
                SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                _WATCH_HORIZON_SECONDS,
                SCENE_UPLOAD_LATE_WATCH_MARKER,
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
                    f"SELECT {_ARTIFACT_COLUMNS} FROM creator_artifacts "
                    f"WHERE id = $1 AND {_watch_horizon_elapsed(2, 3)} FOR UPDATE SKIP LOCKED",
                    artifact_id,
                    _WATCH_HORIZON_SECONDS,
                    SCENE_UPLOAD_LATE_WATCH_MARKER,
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
