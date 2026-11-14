"""Run/key ownership and global reclaim fencing against migrated PostgreSQL."""

from pathlib import Path

import asyncpg
import pytest
from creator_service import db, postgres_visual_asset_storage
from creator_service.artifact_deletion_retry import retry_failed_deletions
from creator_service.object_storage import LocalStorageBackend
from creator_service.postgres_visual_asset_storage import PostgresVisualAssetStorage
from creator_service.rejected_scene_cleanup import (
    SCENE_UPLOAD_LATE_WATCH_MARKER,
    rejected_scene_cleanup_service,
)
from creator_service.visual_asset_service import SceneAssetAcceptanceRejected

from project_deletion_support import DeletionDatabase

pytest_plugins = ("project_deletion_support",)


@pytest.fixture(autouse=True)
async def boundary_runs(
    deletion_database: DeletionDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real workspace/project relations: runs 1 and 11 share ownership, run 21 does not.
    await deletion_database.pool.execute(
        "INSERT INTO creator_runs (id, project_id, workspace_id) "
        "VALUES (1, 1, 1), (11, 1, 1), (21, 2, 2)",
    )
    monkeypatch.setattr(postgres_visual_asset_storage, "get_pool", db.get_pool)


@pytest.fixture
def objects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalStorageBackend:
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    return LocalStorageBackend(str(tmp_path / "objects"))


async def intent(database: DeletionDatabase, run_id: int, path: Path) -> int:
    """Register through the production adapter with the run's actual ownership."""
    return await rejected_scene_cleanup_service.record_intent(
        run_id=run_id,
        workspace_id=await database.pool.fetchval(
            "SELECT workspace_id FROM creator_runs WHERE id = $1", run_id,
        ),
        project_id=await database.pool.fetchval(
            "SELECT project_id FROM creator_runs WHERE id = $1", run_id,
        ),
        storage_key=f"{run_id}/scenes/s1.png",
        local_path=str(path), storage_provider="local", content_type="image/png",
    )


async def expire(database: DeletionDatabase, intent_id: int) -> None:
    await database.pool.execute(
        "UPDATE creator_artifacts SET created_at = NOW() - INTERVAL '6 minutes' WHERE id = $1",
        intent_id,
    )


async def artifact(database: DeletionDatabase, intent_id: int) -> asyncpg.Record | None:
    return await database.pool.fetchrow(
        "SELECT * FROM creator_artifacts WHERE id = $1", intent_id,
    )


@pytest.mark.parametrize(
    ("target_run", "target_key"),
    [(11, "1/scenes/s1.png"), (21, "1/scenes/s1.png"), (1, "1/scenes/other.png")],
    ids=["another-run", "another-workspace", "another-key"],
)
async def test_acceptance_rejects_foreign_intent_and_owner_can_still_claim_it(
    deletion_database: DeletionDatabase, target_run: int, target_key: str,
) -> None:
    # Given A's unfenced intent, with a different run/workspace or key as claimant.
    database = deletion_database
    intent_id = await intent(database, 1, Path("unused.png"))
    original = await artifact(database, intent_id)
    storage = PostgresVisualAssetStorage()

    # When the claimant submits A's intent id through the real acceptance transaction.
    with pytest.raises(SceneAssetAcceptanceRejected) as rejected:
        await storage.save_asset({
            "run_id": target_run, "scene_id": "s1", "storage_key": target_key,
            "asset_path": "unused.png",
            "upload_intent_id": intent_id, "is_active": True,
        })

    # Then rejection is side-effect free, not a destructive failed claim.
    assert rejected.value.run_id == target_run
    assert await artifact(database, intent_id) == original
    assert await database.pool.fetchval("SELECT count(*) FROM creator_scene_assets") == 0
    accepted = await storage.save_asset({
        "run_id": 1, "scene_id": "s1", "storage_key": "1/scenes/s1.png",
        "asset_path": "unused.png",
        "upload_intent_id": intent_id, "is_active": True,
    })
    assert (accepted["run_id"], accepted["storage_key"], accepted["is_active"]) == (
        1, "1/scenes/s1.png", True,
    )
    assert await artifact(database, intent_id) is None
    assert await storage.get_asset(accepted["id"]) == accepted


@pytest.mark.parametrize("b_expired", [False, True], ids=["fresh-B", "expired-B-global"])
async def test_global_retry_reclaims_a_and_discards_only_expired_b_stale_intent(
    deletion_database: DeletionDatabase, objects: LocalStorageBackend, b_expired: bool,
) -> None:
    # Given suffix-colliding keys in different runs and an accepted B asset.
    database = deletion_database
    key_a, key_b = "1/scenes/s1.png", "11/scenes/s1.png"
    objects.upload(key_a, b"unaccepted-A")
    objects.upload(key_b, b"accepted-B")
    path_a, path_b = objects._root / key_a, objects._root / key_b
    # Insert B first so a broadened candidate lookup cannot pass by insertion order.
    accepted_b = await PostgresVisualAssetStorage().save_asset({
        "run_id": 11, "scene_id": "s1", "storage_key": key_b,
        "asset_path": str(path_b),
        "storage_provider": "local", "is_active": True,
    })
    intent_b = await intent(database, 11, path_b)
    if b_expired:
        await expire(database, intent_b)
    before_b = await artifact(database, intent_b)
    intent_a = await intent(database, 1, path_a)
    await expire(database, intent_a)

    # When one global production retry runs (there is deliberately no run argument).
    retried = await retry_failed_deletions(5, db.transaction, lambda: objects)

    # Then A alone loses bytes; a fresh B intent is untouched, an expired B is discarded.
    assert retried == 1 + int(b_expired)
    assert not objects.exists(key_a)
    assert objects.download_bytes(key_b) == b"accepted-B"
    assert await PostgresVisualAssetStorage().get_asset(accepted_b["id"]) == accepted_b
    assert await artifact(database, intent_b) == (None if b_expired else before_b)
    watch_a = await artifact(database, intent_a)
    assert watch_a is not None
    assert watch_a["delete_error"] == SCENE_UPLOAD_LATE_WATCH_MARKER
    assert watch_a["delete_failed_at"] is not None
    assert await database.pool.fetchval("SELECT count(*) FROM creator_scene_assets") == 1


async def test_same_key_reference_in_b_cannot_protect_a_or_replace_its_t2_candidate(
    deletion_database: DeletionDatabase, objects: LocalStorageBackend,
) -> None:
    # Given a synthetic exact-key collision; real workers generate run-prefixed keys.
    database = deletion_database
    key = "1/scenes/s1.png"
    objects.upload(key, b"shared-key")
    path = objects._root / key
    accepted_b = await PostgresVisualAssetStorage().save_asset({
        "run_id": 11, "scene_id": "s1", "storage_key": key,
        "asset_path": str(path),
        "storage_provider": "local", "is_active": True,
    })
    # Given B's fresh intent AFTER acceptance; its lower id must not replace A in T2.
    intent_b = await intent(database, 11, path)
    await database.pool.execute(
        "UPDATE creator_artifacts SET storage_key = $2 WHERE id = $1", intent_b, key,
    )
    before_b = await artifact(database, intent_b)
    intent_a = await intent(database, 1, path)
    await expire(database, intent_a)

    # When only A is eligible in T1, T2 must reselect that globally unique artifact id.
    retried = await retry_failed_deletions(5, db.transaction, lambda: objects)

    # Then B's reference is NOT A's reference: the guard matches (run, exact key, provider).
    # Local objects have a global key namespace, so this synthetic alias loses its bytes;
    # the production rule preserves B's DB reference, not bytes aliased across run keys.
    assert retried == 1
    assert not objects.exists(key)
    assert await PostgresVisualAssetStorage().get_asset(accepted_b["id"]) == accepted_b
    assert await artifact(database, intent_b) == before_b
    watch_a = await artifact(database, intent_a)
    assert watch_a is not None
    assert watch_a["delete_error"] == SCENE_UPLOAD_LATE_WATCH_MARKER
    assert watch_a["delete_failed_at"] is not None
