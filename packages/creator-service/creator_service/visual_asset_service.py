"""Visual asset service for scene-versioned image storage and selection.

Manages image assets generated for visual-plan scenes.  Each scene can have
multiple asset versions (e.g. regenerations).  Exactly one asset per scene
is marked *active* at any time.

Follows the same Protocol \u2192 InMemory \u2192 Service pattern as
VisualPlanService and ScriptService.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from creator_domain.models.visual_asset import VisualAsset

from .exception_pickling import Reduced, field_values, rebuild_exception


@dataclass(frozen=True, slots=True)
class SceneAssetAcceptanceRejected(RuntimeError):
    run_id: int

    def __str__(self) -> str:
        return f"scene asset acceptance rejected for run {self.run_id}"

    def __reduce__(self) -> Reduced:
        return rebuild_exception, (type(self), field_values(self))

# ---------------------------------------------------------------------------
# Storage Protocol
# ---------------------------------------------------------------------------


class VisualAssetStorageBackend(Protocol):
    async def save_asset(self, row: dict[str, Any]) -> dict[str, Any]:
        """Persist a visual asset row and return stored row with id assigned.

        The storage backend atomically:
        1. Allocates the next version number for ``(run_id, scene_id)``.
        2. If ``row["is_active"]`` is True, deactivates any previously-active
           asset for the same ``(run_id, scene_id)``.
        3. Inserts the new row.

        All three steps MUST execute under a single lock/transaction so that
        concurrent creates cannot produce two active assets for one scene.

        Callers MUST NOT include ``version`` in *row*; the returned dict
        MUST contain the allocated ``version``.
        """
        ...

    async def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        """Fetch a single asset by id."""
        ...

    async def delete_asset(self, asset_id: int) -> bool:
        ...

    async def list_assets_by_scene(self, run_id: int, scene_id: str) -> list[dict[str, Any]]:
        """List all asset versions for a scene, newest first."""
        ...

    async def list_assets_by_run(self, run_id: int) -> list[dict[str, Any]]:
        """List all assets for a run, grouped by scene_id then newest first."""
        ...

    async def set_active(self, run_id: int, scene_id: str, asset_id: int) -> bool:
        """Mark *asset_id* as active for the given scene and deactivate others.

        Returns True if the asset was found and activated, False otherwise.
        """
        ...

    async def get_active_asset(self, run_id: int, scene_id: str) -> dict[str, Any] | None:
        """Fetch the currently active asset for a scene."""
        ...


# ---------------------------------------------------------------------------
# In-Memory Storage
# ---------------------------------------------------------------------------


class InMemoryVisualAssetStorage:
    """In-memory storage with atomic per-(run_id, scene_id) version allocation."""

    def __init__(
        self,
        acceptance_guard: Callable[[int], Awaitable[bool]] | None = None,
        upload_intent_claim: Callable[[int, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._assets: list[dict[str, Any]] = []
        self._next_id = 1
        self._scene_locks: dict[tuple[int, str], asyncio.Lock] = {}
        self._by_idempotency_key: dict[str, dict[str, Any]] = {}
        self._acceptance_guard = acceptance_guard
        self._upload_intent_claim = upload_intent_claim

    async def save_asset(self, row: dict[str, Any]) -> dict[str, Any]:
        run_id = row["run_id"]
        scene_id = row["scene_id"]
        key = (run_id, scene_id)
        lock = self._scene_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if self._acceptance_guard is not None and not await self._acceptance_guard(run_id):
                raise SceneAssetAcceptanceRejected(run_id=run_id)
            idem_key = row.get("idempotency_key")
            if isinstance(idem_key, str) and idem_key in self._by_idempotency_key:
                existing = self._by_idempotency_key[idem_key]
                if existing["run_id"] != run_id or existing["scene_id"] != scene_id:
                    raise SceneAssetAcceptanceRejected(run_id=run_id)
                return dict(existing)
            upload_intent_id = row.get("upload_intent_id")
            storage_key = row.get("storage_key")
            if isinstance(upload_intent_id, int):
                if not isinstance(storage_key, str) or (
                    self._upload_intent_claim is not None
                    and not await self._upload_intent_claim(upload_intent_id, storage_key)
                ):
                    raise SceneAssetAcceptanceRejected(run_id=run_id)
            # Deactivate previous active if this asset should be active.
            if row.get("is_active", False):
                for a in self._assets:
                    if (
                        a["run_id"] == run_id
                        and a["scene_id"] == scene_id
                        and a.get("is_active", False)
                    ):
                        a["is_active"] = False
            # Compute next version for this scene
            scene_assets = [
                a for a in self._assets if a["run_id"] == run_id and a["scene_id"] == scene_id
            ]
            next_version = max((a["version"] for a in scene_assets), default=0) + 1
            now = datetime.now(timezone.utc)
            saved = {
                "id": self._next_id,
                "version": next_version,
                "created_at": now,
                **row,
            }
            self._next_id += 1
            self._assets.append(saved)
            if isinstance(idem_key, str):
                self._by_idempotency_key[idem_key] = saved
        return dict(saved)

    async def get_asset(self, asset_id: int) -> dict[str, Any] | None:
        for a in self._assets:
            if a["id"] == asset_id:
                return dict(a)
        return None

    async def delete_asset(self, asset_id: int) -> bool:
        for asset in self._assets:
            if asset["id"] == asset_id:
                self._assets.remove(asset)
                idempotency_key = asset.get("idempotency_key")
                if isinstance(idempotency_key, str):
                    self._by_idempotency_key.pop(idempotency_key, None)
                return True
        return False

    async def list_assets_by_scene(self, run_id: int, scene_id: str) -> list[dict[str, Any]]:
        scene_assets = [
            a for a in self._assets if a["run_id"] == run_id and a["scene_id"] == scene_id
        ]
        return [dict(a) for a in sorted(scene_assets, key=lambda x: x["version"], reverse=True)]

    async def list_assets_by_run(self, run_id: int) -> list[dict[str, Any]]:
        run_assets = [a for a in self._assets if a["run_id"] == run_id]
        # Sort by scene_id ASC, then version DESC within each scene
        return [
            dict(a)
            for a in sorted(
                run_assets,
                key=lambda x: (x["scene_id"], -x["version"]),
            )
        ]

    async def set_active(self, run_id: int, scene_id: str, asset_id: int) -> bool:
        key = (run_id, scene_id)
        lock = self._scene_locks.setdefault(key, asyncio.Lock())
        async with lock:
            found = False
            for a in self._assets:
                if a["run_id"] == run_id and a["scene_id"] == scene_id:
                    if a["id"] == asset_id:
                        a["is_active"] = True
                        found = True
                    else:
                        a["is_active"] = False
            return found

    async def get_active_asset(self, run_id: int, scene_id: str) -> dict[str, Any] | None:
        for a in self._assets:
            if a["run_id"] == run_id and a["scene_id"] == scene_id and a.get("is_active", False):
                return dict(a)
        return None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class VisualAssetService:
    def __init__(self, storage: VisualAssetStorageBackend | None = None) -> None:
        self.storage = storage if storage is not None else InMemoryVisualAssetStorage()

    # -- Create -------------------------------------------------------------

    async def create_asset(
        self,
        run_id: int,
        scene_id: str,
        asset_path: str,
        *,
        prompt_snapshot: str | None = None,
        model_used: str | None = None,
        provider_type: str | None = None,
        storage_provider: str | None = None,
        storage_key: str | None = None,
        is_active: bool = True,
        idempotency_key: str | None = None,
        upload_intent_id: int | None = None,
    ) -> VisualAsset:
        """Create a new asset version for a scene.

        If *is_active* is True (default), any previously-active asset for
        the same scene is deactivated first.  Version allocation AND
        deactivation are delegated to the storage backend as a single
        atomic operation (under one lock) to prevent race conditions
        between concurrent creates.
        """
        row = {
            "run_id": run_id,
            "scene_id": scene_id,
            "asset_path": asset_path,
            "prompt_snapshot": prompt_snapshot,
            "model_used": model_used,
            "provider": provider_type,  # DB column is 'provider'
            "storage_provider": storage_provider,
            "storage_key": storage_key,
            "is_active": is_active,
            "idempotency_key": idempotency_key,
            "upload_intent_id": upload_intent_id,
        }
        # save_asset atomically: allocates version, deactivates previous
        # active (when is_active=True), and inserts — all under one lock.
        saved = await self.storage.save_asset(row)

        return self._row_to_asset(saved)

    # -- Select active ------------------------------------------------------

    async def select_active(
        self,
        run_id: int,
        scene_id: str,
        asset_id: int,
    ) -> VisualAsset:
        """Set the given asset as active for its scene.

        Raises ValueError if the asset is not found or doesn't belong
        to the specified run/scene.
        """
        asset_row = await self.storage.get_asset(asset_id)
        if asset_row is None:
            raise ValueError(f"Asset {asset_id} not found")
        if asset_row["run_id"] != run_id or asset_row["scene_id"] != scene_id:
            raise ValueError(f"Asset {asset_id} does not belong to run {run_id} scene '{scene_id}'")

        success = await self.storage.set_active(run_id, scene_id, asset_id)
        if not success:
            raise ValueError(f"Failed to activate asset {asset_id}")

        # Re-fetch to get updated is_active state
        updated = await self.storage.get_asset(asset_id)
        if updated is None:
            raise ValueError(f"Asset {asset_id} not found")
        return self._row_to_asset(updated)

    # -- Reads --------------------------------------------------------------

    async def list_by_scene(self, run_id: int, scene_id: str) -> list[VisualAsset]:
        """List all asset versions for a scene, newest first."""
        rows = await self.storage.list_assets_by_scene(run_id, scene_id)
        return [self._row_to_asset(r) for r in rows]

    async def list_by_run(self, run_id: int) -> dict[str, list[VisualAsset]]:
        """List all assets for a run, grouped by scene_id.

        Returns a dict mapping scene_id \u2192 list[VisualAsset] (newest first
        within each scene).
        """
        rows = await self.storage.list_assets_by_run(run_id)
        grouped: dict[str, list[VisualAsset]] = {}
        for row in rows:
            asset = self._row_to_asset(row)
            grouped.setdefault(asset.scene_id, []).append(asset)
        return grouped

    async def get_active_asset(self, run_id: int, scene_id: str) -> VisualAsset | None:
        """Get the currently active asset for a scene."""
        row = await self.storage.get_active_asset(run_id, scene_id)
        if row is None:
            return None
        return self._row_to_asset(row)

    async def get_asset(self, asset_id: int) -> VisualAsset | None:
        """Get a single asset by id."""
        row = await self.storage.get_asset(asset_id)
        if row is None:
            return None
        return self._row_to_asset(row)

    # -- Internal -----------------------------------------------------------

    @staticmethod
    def _row_to_asset(row: dict[str, Any]) -> VisualAsset:
        """Convert a storage row to a VisualAsset domain model.

        Handles the provider\u2192provider_type column mapping.
        """
        return VisualAsset.from_row(row)


def _create_storage() -> VisualAssetStorageBackend:
    import os

    if os.getenv("DATABASE_URL"):
        from .postgres_visual_asset_storage import PostgresVisualAssetStorage

        return PostgresVisualAssetStorage()

    async def accepts(run_id: int) -> bool:
        from .project_service import project_service
        from .run_service import run_service

        run = await run_service.storage.get_run(run_id)
        if run is None or run.get("status") == "cancelled":
            return False
        project_id = run.get("project_id")
        if not isinstance(project_id, int):
            return False
        project = await project_service.storage.fetch_project(project_id)
        return project is not None and project.get("status") != "deleting"

    return InMemoryVisualAssetStorage(acceptance_guard=accepts)


visual_asset_service = VisualAssetService(_create_storage())
