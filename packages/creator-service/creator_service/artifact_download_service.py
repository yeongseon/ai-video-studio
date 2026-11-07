from __future__ import annotations

import logging
import os
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.artifact_storage_integration import get_artifact_download_path
from creator_service.confined_directory_deletion import (
    delete_confined_file,
    delete_direct_child_tree,
)
from creator_service.object_storage import get_storage_backend
from creator_service.run_object_deletion import delete_run_object
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)

from .db import execute, fetch_all, fetch_one, transaction

logger = logging.getLogger(__name__)


def _key_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class ArtifactDownloadStorageBackend(Protocol):
    async def get_artifact_by_id(self, artifact_id: int) -> dict[str, Any] | None: ...


class InMemoryArtifactDownloadStorage:
    def __init__(self) -> None:
        self._artifacts: list[dict[str, Any]] = []
        self._next_id = 1

    async def save_artifact(self, row: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        saved = {
            "id": self._next_id,
            "created_at": now,
            **row,
        }
        self._next_id += 1
        self._artifacts.append(saved)
        return dict(saved)

    async def get_artifact_by_id(self, artifact_id: int) -> dict[str, Any] | None:
        for artifact in self._artifacts:
            if artifact["id"] == artifact_id:
                return dict(artifact)
        return None


class PostgresArtifactDownloadStorage:
    async def get_artifact_by_id(self, artifact_id: int) -> dict[str, Any] | None:
        return await fetch_one(
            "SELECT * FROM creator_artifacts WHERE id = $1",
            artifact_id,
        )


class ArtifactDownloadService:
    def __init__(self, storage: ArtifactDownloadStorageBackend | None = None) -> None:
        self.storage = storage if storage is not None else InMemoryArtifactDownloadStorage()

    async def get_artifact_by_id(self, artifact_id: int) -> dict[str, Any] | None:
        return await self.storage.get_artifact_by_id(artifact_id)

    async def delete_artifacts_for_run(self, run_id: int) -> int:
        artifacts = await fetch_all(
            "SELECT id, COALESCE(NULLIF(storage_key, ''), file_path) AS storage_key, file_path, metadata_json, "
            "storage_provider AS object_storage_provider FROM creator_artifacts WHERE run_id = $1 "
            "UNION ALL "
            "SELECT NULL::integer AS id, COALESCE(NULLIF(storage_key, ''), asset_path) AS storage_key, "
            "asset_path AS file_path, NULL::text AS metadata_json, "
            "storage_provider AS object_storage_provider FROM creator_scene_assets WHERE run_id = $1",
            run_id,
        )
        # Mark all artifacts as deletion-requested
        if artifacts:
            await execute(
                "UPDATE creator_artifacts SET delete_requested_at = NOW() "
                "WHERE run_id = $1 AND delete_requested_at IS NULL",
                run_id,
            )

        backend = get_storage_backend()

        deleted_ids: list[int] = []
        failed_count = 0

        for artifact in artifacts:
            key = artifact.get("storage_key")
            artifact_id = artifact.get("id")
            if not isinstance(key, str) or not key:
                if isinstance(artifact_id, int):
                    deleted_ids.append(artifact_id)
                continue
            try:
                provider = artifact.get("object_storage_provider")
                if artifact.get("metadata_json") in {
                    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
                    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                }:
                    local_path = artifact.get("file_path")
                    if not isinstance(local_path, str):
                        raise OSError("rejected upload cleanup path missing")
                    delete_run_object(
                        key,
                        provider if isinstance(provider, str) else None,
                        backend,
                    )
                    delete_confined_file(Path(local_path))
                else:
                    delete_run_object(key, provider if isinstance(provider, str) else None, backend)
                if isinstance(artifact_id, int):
                    deleted_ids.append(artifact_id)
            except Exception as exc:
                failed_count += 1
                provider_name = provider if isinstance(provider, str) else "configured"
                fingerprint = _key_fingerprint(key)
                logger.error(
                    "Artifact deletion failed",
                    extra={
                        "run_id": run_id,
                        "storage_provider": provider_name,
                        "key_fingerprint": fingerprint,
                        "error_type": type(exc).__name__,
                    },
                )
                if isinstance(artifact_id, int):
                    await self._record_delete_failure(
                        artifact_id,
                        provider_name,
                        fingerprint,
                    )

        if deleted_ids:
            await execute(
                "DELETE FROM creator_artifacts WHERE id = ANY($1::int[])",
                deleted_ids,
            )

        if failed_count:
            logger.warning(
                "%d artifact(s) for run_id=%s could not be deleted from storage; DB rows retained for retry",
                failed_count,
                run_id,
            )

        if failed_count or os.getenv("STORAGE_BACKEND", "local") != "local":
            return failed_count

        artifact_root = Path(os.getenv("ARTIFACT_ROOT", "data/artifacts"))
        try:
            delete_direct_child_tree(artifact_root, str(run_id))
        except OSError:
            logger.exception("Failed deleting local run directory for run_id=%s", run_id)
            return 1
        return failed_count


    @staticmethod
    async def _record_delete_failure(
        artifact_id: int,
        provider: str,
        fingerprint: str,
    ) -> None:
        """Persist failure metadata for a single artifact."""
        await execute(
            "UPDATE creator_artifacts "
            "SET delete_failed_at = NOW(), "
            "    delete_error = $2, "
            "    delete_retry_count = COALESCE(delete_retry_count, 0) + 1 "
            "WHERE id = $1",
            artifact_id,
            f"provider={provider}; fingerprint={fingerprint}",
        )

    @staticmethod
    async def record_rejected_cleanup_failure(artifact_id: int, storage_key: str) -> None:
        await ArtifactDownloadService._record_delete_failure(
            artifact_id, "configured", _key_fingerprint(storage_key),
        )

    async def sweep_expired(self, batch_size: int = 500) -> int:
        """Mark expired artifacts for deletion.

        Rows whose ``expires_at`` has passed and that haven't already been
        scheduled for deletion get ``delete_requested_at = NOW()``. The actual
        storage + DB row removal is handled by the existing
        ``retry_failed_deletions`` flow (``FOR UPDATE SKIP LOCKED``).

        Returns the number of rows newly marked for deletion.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        result = await execute(
            "UPDATE creator_artifacts "
            "SET delete_requested_at = NOW() "
            "WHERE id IN ("
            "    SELECT id FROM creator_artifacts "
            "    WHERE expires_at IS NOT NULL"
            "      AND expires_at < NOW()"
            "      AND delete_requested_at IS NULL"
            "    LIMIT $1"
            ")",
            batch_size,
        )
        # db.execute() returns the asyncpg status string directly ("UPDATE N").
        status = result if isinstance(result, str) else (getattr(result, "statusmessage", "") or "")
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (TypeError, ValueError):
            return 0


    async def retry_failed_deletions(self, max_retries: int = 5) -> int:
        """Reattempt deletion for artifacts that previously failed.

        Each candidate is selected and processed in its own transaction so
        its ``FOR UPDATE SKIP LOCKED`` lock remains held through storage I/O.
        The 500-row pass limit bounds both work and lock duration to one row.

        Also picks up *stranded* rows — those with ``delete_requested_at``
        set but no ``delete_failed_at`` (e.g. process crashed after storage
        delete succeeded but before the DB row was removed).

        Returns the number of artifacts successfully deleted in this pass.
        """
        if max_retries < 1:
            raise ValueError("max_retries must be >= 1")
        return await retry_failed_deletions(max_retries, transaction, get_storage_backend)


def _create_storage() -> ArtifactDownloadStorageBackend:
    import os

    if os.getenv("DATABASE_URL"):
        return PostgresArtifactDownloadStorage()
    return InMemoryArtifactDownloadStorage()


artifact_download_service = ArtifactDownloadService(_create_storage())


def resolve_artifact_download(key: str) -> str:
    return get_artifact_download_path(key)


def read_artifact_bytes(key: str) -> bytes:
    backend = get_storage_backend()
    if not backend.exists(key):
        raise FileNotFoundError(key)
    return backend.download_bytes(key)
