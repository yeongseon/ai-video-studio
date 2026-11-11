from __future__ import annotations

from pathlib import Path

from .db import execute, transaction
from .object_storage import get_storage_backend

REJECTED_SCENE_UPLOAD_CLEANUP_MARKER = "rejected_scene_upload_cleanup"
SCENE_UPLOAD_CLEANUP_INTENT_MARKER = "scene_upload_cleanup_intent"
INTERNAL_CLEANUP_MARKERS = (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)


class RejectedSceneCleanupService:
    async def record_intent(
        self,
        *,
        run_id: int,
        workspace_id: int | None,
        project_id: int | None,
        storage_key: str,
        local_path: str,
        storage_provider: str,
        content_type: str,
    ) -> int:
        async with transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock($1::int, hashtext($2))",
                run_id,
                storage_key,
            )
            existing = await conn.fetchrow(
                "SELECT id FROM creator_artifacts "
                "WHERE run_id = $1 AND storage_key = $2 AND metadata_json = $3",
                run_id,
                storage_key,
                SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            )
            if existing is not None:
                return int(existing["id"])
            row = await conn.fetchrow(
                "INSERT INTO creator_artifacts ("
                "run_id, workspace_id, project_id, artifact_type, scene_id, file_path, "
                "storage_key, storage_provider, content_type, metadata_json, delete_requested_at"
                ") VALUES ($1, $2, $3, 'visual_asset', $4, $5, $6, $7, $8, $9, NOW()) "
                "RETURNING id",
                run_id,
                workspace_id,
                project_id,
                Path(local_path).stem,
                local_path,
                storage_key,
                storage_provider,
                content_type,
                SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            )
        return int(row["id"])

    async def record(
        self,
        *,
        run_id: int,
        workspace_id: int | None,
        project_id: int | None,
        storage_key: str,
        local_path: str,
        storage_provider: str,
        content_type: str,
        size_bytes: int,
        checksum: str,
    ) -> int:
        async with transaction() as conn:
            row = await conn.fetchrow(
                "INSERT INTO creator_artifacts ("
                "run_id, workspace_id, project_id, artifact_type, scene_id, file_path, "
                "storage_key, storage_provider, content_type, size_bytes, checksum, "
                "metadata_json, delete_requested_at"
                ") VALUES ($1, $2, $3, 'visual_asset', $4, $5, $6, $7, $8, $9, $10, $11, NOW()) "
                "RETURNING id",
                run_id,
                workspace_id,
                project_id,
                Path(local_path).stem,
                local_path,
                storage_key,
                storage_provider,
                content_type,
                size_bytes,
                checksum,
                REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
            )
        return int(row["id"])

    async def cleanup(self, record_id: int, storage_key: str, local_path: str) -> None:
        get_storage_backend().delete(storage_key)
        Path(local_path).unlink(missing_ok=True)
        await execute("DELETE FROM creator_artifacts WHERE id = $1", record_id)


rejected_scene_cleanup_service = RejectedSceneCleanupService()
