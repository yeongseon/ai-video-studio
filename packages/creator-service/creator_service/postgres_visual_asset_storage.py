from __future__ import annotations

from typing import Any

import asyncpg

from .db import fetch_all, fetch_one, get_pool
from .rejected_scene_cleanup import SCENE_UPLOAD_CLEANUP_INTENT_MARKER
from .visual_asset_service import SceneAssetAcceptanceRejected


class PostgresVisualAssetStorage:
    async def save_asset(self, row: dict[str, Any]) -> dict[str, Any]:
        run_id = row["run_id"]
        scene_id = row["scene_id"]
        is_active = bool(row.get("is_active", False))
        idempotency_key = row.get("idempotency_key")
        upload_intent_id = row.get("upload_intent_id")
        storage_key = row.get("storage_key")

        pool = await get_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                # One lock order for every acceptance path:
                # project -> run -> upload intent -> scene advisory lock.
                # Deletion commits its fence before cleanup, and reclaim fences an intent
                # before it does any storage I/O, so this order cannot form a cycle.
                project = await connection.fetchrow(
                    "SELECT p.status FROM creator_projects p "
                    "JOIN creator_runs r ON r.project_id = p.id WHERE r.id = $1 FOR SHARE OF p",
                    run_id,
                )
                run = await connection.fetchrow(
                    "SELECT status FROM creator_runs WHERE id = $1 FOR SHARE",
                    run_id,
                )
                if (
                    project is None
                    or run is None
                    or project["status"] == "deleting"
                    or run["status"] == "cancelled"
                ):
                    raise SceneAssetAcceptanceRejected(run_id=run_id)

                # NOWAIT: never queue behind reclaim's storage I/O while holding the parent
                # share locks; a busy intent means reclaim owns it, so acceptance fails closed.
                if isinstance(upload_intent_id, int):
                    intents = await connection.fetch(
                        "SELECT id, delete_failed_at FROM creator_artifacts "
                        "WHERE id = $1 AND run_id = $2 AND storage_key = $3 "
                        "AND metadata_json = $4 FOR UPDATE NOWAIT",
                        upload_intent_id,
                        run_id,
                        storage_key,
                        SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                    )
                elif isinstance(storage_key, str):
                    intents = await connection.fetch(
                        "SELECT id, delete_failed_at FROM creator_artifacts "
                        "WHERE run_id = $1 AND storage_key = $2 AND metadata_json = $3 "
                        "FOR UPDATE NOWAIT",
                        run_id,
                        storage_key,
                        SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
                    )
                else:
                    intents = []

                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1::int, hashtext($2))",
                    run_id,
                    scene_id,
                )

                if isinstance(idempotency_key, str):
                    # Definitive under the scene lock: a concurrent winner has committed.
                    # A replay returns the winner untouched, so a loser's intent is kept
                    # and its object stays discoverable for cleanup.
                    existing = await connection.fetchrow(
                        "SELECT * FROM creator_scene_assets WHERE idempotency_key = $1",
                        idempotency_key,
                    )
                    if existing is not None:
                        if existing["run_id"] != run_id or existing["scene_id"] != scene_id:
                            raise SceneAssetAcceptanceRejected(run_id=run_id)
                        return dict(existing)

                # A fenced intent means reclaim may already have deleted the object.
                if any(intent["delete_failed_at"] is not None for intent in intents):
                    raise SceneAssetAcceptanceRejected(run_id=run_id)
                if isinstance(upload_intent_id, int) and not intents:
                    raise SceneAssetAcceptanceRejected(run_id=run_id)

                version_row = await connection.fetchrow(
                    """
                    SELECT COALESCE(MAX(version), 0) + 1 AS next_version
                    FROM creator_scene_assets
                    WHERE run_id = $1 AND scene_id = $2
                    """,
                    run_id,
                    scene_id,
                )
                next_version = int(version_row["next_version"]) if version_row is not None else 1

                if is_active:
                    await connection.execute(
                        """
                        UPDATE creator_scene_assets
                        SET is_active = false
                        WHERE run_id = $1 AND scene_id = $2 AND is_active = true
                        """,
                        run_id,
                        scene_id,
                    )

                saved = await connection.fetchrow(
                    """
                    INSERT INTO creator_scene_assets (
                        run_id,
                        scene_id,
                        version,
                        asset_path,
                        prompt_snapshot,
                        model_used,
                        provider,
                        storage_provider,
                        storage_key,
                        is_active,
                        idempotency_key
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING *
                    """,
                    run_id,
                    scene_id,
                    next_version,
                    row.get("asset_path"),
                    row.get("prompt_snapshot"),
                    row.get("model_used"),
                    row.get("provider"),
                    row.get("storage_provider"),
                    storage_key,
                    is_active,
                    idempotency_key,
                )
                if saved is None:
                    # Only a concurrent writer under another scene's lock can win this key.
                    # Raising rolls back the deactivation above and keeps the intent.
                    raise SceneAssetAcceptanceRejected(run_id=run_id)
                if intents:
                    await connection.execute(
                        "DELETE FROM creator_artifacts WHERE id = ANY($1::int[])",
                        [int(intent["id"]) for intent in intents],
                    )
        except asyncpg.exceptions.LockNotAvailableError:
            raise SceneAssetAcceptanceRejected(run_id=run_id) from None
        return dict(saved)

    async def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        return await fetch_one("SELECT * FROM creator_scene_assets WHERE id = $1", asset_id)

    async def delete_asset(self, asset_id: int) -> bool:
        pool = await get_pool()
        result = await pool.execute(
            "DELETE FROM creator_scene_assets WHERE id = $1",
            asset_id,
        )
        return result == "DELETE 1"

    async def list_assets_by_scene(self, run_id: int, scene_id: str) -> list[dict[str, Any]]:
        return await fetch_all(
            """
            SELECT *
            FROM creator_scene_assets
            WHERE run_id = $1 AND scene_id = $2
            ORDER BY version DESC
            """,
            run_id,
            scene_id,
        )

    async def list_assets_by_run(self, run_id: int) -> list[dict[str, Any]]:
        return await fetch_all(
            """
            SELECT *
            FROM creator_scene_assets
            WHERE run_id = $1
            ORDER BY scene_id ASC, version DESC
            """,
            run_id,
        )

    async def set_active(self, run_id: int, scene_id: str, asset_id: int) -> bool:
        pool = await get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1::int, hashtext($2))",
                run_id,
                scene_id,
            )
            await connection.execute(
                """
                    UPDATE creator_scene_assets
                    SET is_active = false
                    WHERE run_id = $1 AND scene_id = $2
                    """,
                run_id,
                scene_id,
            )
            target = await connection.fetchrow(
                """
                    UPDATE creator_scene_assets
                    SET is_active = true
                    WHERE run_id = $1 AND scene_id = $2 AND id = $3
                    RETURNING id
                    """,
                run_id,
                scene_id,
                asset_id,
            )
        return target is not None

    async def get_active_asset(self, run_id: int, scene_id: str) -> dict[str, Any] | None:
        return await fetch_one(
            """
            SELECT *
            FROM creator_scene_assets
            WHERE run_id = $1 AND scene_id = $2 AND is_active = true
            LIMIT 1
            """,
            run_id,
            scene_id,
        )
