from __future__ import annotations

from pathlib import Path

from .confined_directory_deletion import delete_artifact_file
from .db import execute, transaction
from .object_storage import get_storage_backend

REJECTED_SCENE_UPLOAD_CLEANUP_MARKER = "rejected_scene_upload_cleanup"
SCENE_UPLOAD_CLEANUP_INTENT_MARKER = "scene_upload_cleanup_intent"
INTERNAL_CLEANUP_MARKERS = (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)
# Stored in delete_error (not metadata_json): a watched intent keeps its intent marker.
SCENE_UPLOAD_LATE_WATCH_MARKER = "scene_upload_late_watch"
SCENE_ID_COLUMN_WIDTH = 100


def bookkeeping_scene_id(local_path: str) -> str:
    """Fit the generated filename stem into creator_artifacts.scene_id (VARCHAR(100)).

    The stem is ``<scene>-<delivery owner>-<uuid>``, which exceeds the column for ordinary
    scene ids once a real task id is included. The tail keeps the per-attempt UUID.
    """
    return Path(local_path).stem[-SCENE_ID_COLUMN_WIDTH:]


class SceneUploadIntentRejected(RuntimeError):
    """Plain exception: frozen slotted dataclasses break under contextlib's __traceback__ writes."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"scene upload intent rejected for run {run_id}")
        self.run_id = run_id


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
            # Same parent fence as acceptance: once deletion or cancellation has committed,
            # artifact enumeration may already be past this run, so no new upload may start.
            project = await conn.fetchrow(
                "SELECT p.status FROM creator_projects p "
                "JOIN creator_runs r ON r.project_id = p.id WHERE r.id = $1 FOR SHARE OF p",
                run_id,
            )
            run = await conn.fetchrow(
                "SELECT status FROM creator_runs WHERE id = $1 FOR SHARE",
                run_id,
            )
            if (
                project is None
                or run is None
                or project["status"] == "deleting"
                or run["status"] == "cancelled"
            ):
                raise SceneUploadIntentRejected(run_id)
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
                bookkeeping_scene_id(local_path),
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
                bookkeeping_scene_id(local_path),
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
        delete_artifact_file(Path(local_path))
        await execute("DELETE FROM creator_artifacts WHERE id = $1", record_id)


rejected_scene_cleanup_service = RejectedSceneCleanupService()
