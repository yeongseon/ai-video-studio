import pytest
from creator_service.admin_service import AdminService
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    PostgresArtifactDownloadStorage,
)
from creator_service.rejected_scene_cleanup import (
    REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
    SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
)

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)

_RUN_ID = 956


async def _insert_artifact(
    database: DeletionDatabase,
    artifact_type: str,
    scene_id: str | None,
    storage_key: str,
    metadata_json: str | None,
) -> int:
    return int(
        await database.pool.fetchval(
            """
            INSERT INTO creator_artifacts (
                run_id, workspace_id, project_id, artifact_type, scene_id, file_path,
                storage_key, storage_provider, content_type, metadata_json,
                delete_requested_at
            ) VALUES (
                $1, 1, 1, $2, $3, $4, $4, 'local', 'application/octet-stream', $5,
                CASE WHEN $5::text IS NULL THEN NULL ELSE NOW() END
            ) RETURNING id
            """,
            _RUN_ID,
            artifact_type,
            scene_id,
            storage_key,
            metadata_json,
        )
    )


@pytest.fixture
async def seeded_rows(deletion_database: DeletionDatabase) -> dict[str, int]:
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) VALUES ($1, 1, 1)",
        _RUN_ID,
    )
    return {
        "ordinary": await _insert_artifact(
            deletion_database, "script", None, "956/script.json", None,
        ),
        "intent": await _insert_artifact(
            deletion_database,
            "visual_asset",
            "intent",
            "956/scenes/intent.png",
            SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
        ),
        "rejected": await _insert_artifact(
            deletion_database,
            "visual_asset",
            "rejected",
            "956/scenes/rejected.png",
            REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        ),
    }


@pytest.mark.usefixtures("seeded_rows")
async def test_admin_artifact_inventory_ignores_internal_cleanup_rows(
    deletion_database: DeletionDatabase,
) -> None:
    # Given a NULL-metadata ordinary row plus pending-upload bookkeeping rows typed visual_asset.
    # When the admin stage check inventories the run's artifact types.
    async with deletion_database.pool.acquire() as connection:
        artifact_types = await AdminService()._get_run_artifact_types(_RUN_ID, connection)

    # Then bookkeeping rows are invisible and the NULL-metadata artifact is still counted.
    assert artifact_types == {"script"}


@pytest.mark.parametrize("label", ["intent", "rejected"])
async def test_postgres_download_lookup_hides_internal_cleanup_rows(
    seeded_rows: dict[str, int], label: str,
) -> None:
    # Given the production service over the real PostgreSQL storage.
    service = ArtifactDownloadService(PostgresArtifactDownloadStorage())

    # When each row is requested by id.
    ordinary = await service.get_artifact_by_id(seeded_rows["ordinary"])
    internal = await service.get_artifact_by_id(seeded_rows[label])

    # Then only the ordinary artifact is addressable.
    assert ordinary is not None
    assert ordinary["id"] == seeded_rows["ordinary"]
    assert internal is None
