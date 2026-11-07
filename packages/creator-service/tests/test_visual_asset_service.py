import asyncio

import pytest
from creator_service.visual_asset_service import (
    InMemoryVisualAssetStorage,
    SceneAssetAcceptanceRejected,
    VisualAssetService,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def storage() -> InMemoryVisualAssetStorage:
    return InMemoryVisualAssetStorage()


@pytest.fixture
def service(storage: InMemoryVisualAssetStorage) -> VisualAssetService:
    return VisualAssetService(storage)


@pytest.mark.asyncio
@pytest.mark.parametrize("project_status,run_status", [("deleting", "running"), ("draft", "cancelled")])
async def test_memory_storage_rejects_assets_after_deletion_barrier(
    project_status: str,
    run_status: str,
) -> None:
    async def accept(_run_id: int) -> bool:
        return project_status != "deleting" and run_status != "cancelled"

    service = VisualAssetService(InMemoryVisualAssetStorage(acceptance_guard=accept))

    with pytest.raises(SceneAssetAcceptanceRejected):
        await service.create_asset(1, "scene-1", "/new.png")

    assert await service.list_by_scene(1, "scene-1") == []


# -- create_asset ---------------------------------------------------------------


def test_create_asset_version_1(service: VisualAssetService) -> None:
    asset = run(
        service.create_asset(
            run_id=1,
            scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v1.png",
            prompt_snapshot="A dramatic opening shot",
            model_used="stable-diffusion-v1-5",
            provider_type="comfyui",
        )
    )

    assert asset.run_id == 1
    assert asset.scene_id == "sc-1"
    assert asset.version == 1
    assert asset.asset_path == "/data/artifacts/1/1/sc-1_v1.png"
    assert asset.prompt_snapshot == "A dramatic opening shot"
    assert asset.model_used == "stable-diffusion-v1-5"
    assert asset.provider_type == "comfyui"
    assert asset.is_active is True


def test_create_asset_increments_version(service: VisualAssetService) -> None:
    run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v1.png",
        )
    )
    second = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v2.png",
        )
    )

    assert second.version == 2


def test_create_asset_inactive(service: VisualAssetService) -> None:
    asset = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v1.png",
            is_active=False,
        )
    )

    assert asset.is_active is False


def test_create_asset_deactivates_previous(service: VisualAssetService) -> None:
    """When a new active asset is created, previous active asset is deactivated."""
    first = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v1.png",
        )
    )
    assert first.is_active is True

    run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v2.png",
        )
    )

    # First asset should now be inactive
    first_now = run(service.get_asset(first.id))
    assert first_now is not None
    assert first_now.is_active is False


def test_create_asset_different_scenes_independent(service: VisualAssetService) -> None:
    """Assets in different scenes have independent version counters."""
    a1 = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/data/artifacts/1/1/sc-1_v1.png",
        )
    )
    a2 = run(
        service.create_asset(
            run_id=1, scene_id="sc-2",
            asset_path="/data/artifacts/1/1/sc-2_v1.png",
        )
    )

    assert a1.version == 1
    assert a2.version == 1


def test_create_asset_provider_type_mapping(service: VisualAssetService) -> None:
    """provider_type param is stored as 'provider' in DB and mapped back."""
    asset = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/path.png",
            provider_type="comfyui",
        )
    )
    assert asset.provider_type == "comfyui"


# -- select_active ---------------------------------------------------------------


def test_select_active_changes_active_asset(service: VisualAssetService) -> None:
    first = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/v1.png",
        )
    )
    second = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/v2.png",
        )
    )

    # Second is now active, select first
    result = run(service.select_active(run_id=1, scene_id="sc-1", asset_id=first.id))
    assert result.is_active is True
    assert result.id == first.id

    # Verify second is now inactive
    second_now = run(service.get_asset(second.id))
    assert second_now is not None
    assert second_now.is_active is False


def test_select_active_idempotent(service: VisualAssetService) -> None:
    """Selecting an already-active asset succeeds without error."""
    asset = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/v1.png",
        )
    )

    result = run(service.select_active(run_id=1, scene_id="sc-1", asset_id=asset.id))
    assert result.is_active is True


def test_select_active_not_found(service: VisualAssetService) -> None:
    with pytest.raises(ValueError, match="Asset 999 not found"):
        run(service.select_active(run_id=1, scene_id="sc-1", asset_id=999))


def test_select_active_wrong_scene(service: VisualAssetService) -> None:
    asset = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/v1.png",
        )
    )

    with pytest.raises(ValueError, match="does not belong to"):
        run(service.select_active(run_id=1, scene_id="sc-other", asset_id=asset.id))


def test_select_active_wrong_run(service: VisualAssetService) -> None:
    asset = run(
        service.create_asset(
            run_id=1, scene_id="sc-1",
            asset_path="/v1.png",
        )
    )

    with pytest.raises(ValueError, match="does not belong to"):
        run(service.select_active(run_id=999, scene_id="sc-1", asset_id=asset.id))


# -- list_by_scene ---------------------------------------------------------------


def test_list_by_scene_newest_first(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v1.png"))
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v2.png"))
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v3.png"))

    assets = run(service.list_by_scene(run_id=1, scene_id="sc-1"))

    assert [a.version for a in assets] == [3, 2, 1]


def test_list_by_scene_empty(service: VisualAssetService) -> None:
    assets = run(service.list_by_scene(run_id=999, scene_id="sc-1"))
    assert assets == []


def test_list_by_scene_excludes_other_scenes(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/sc1.png"))
    run(service.create_asset(run_id=1, scene_id="sc-2", asset_path="/sc2.png"))

    assets = run(service.list_by_scene(run_id=1, scene_id="sc-1"))
    assert len(assets) == 1
    assert assets[0].scene_id == "sc-1"


# -- list_by_run ---------------------------------------------------------------


def test_list_by_run_grouped_by_scene(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/sc1_v1.png"))
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/sc1_v2.png"))
    run(service.create_asset(run_id=1, scene_id="sc-2", asset_path="/sc2_v1.png"))

    grouped = run(service.list_by_run(run_id=1))

    assert set(grouped.keys()) == {"sc-1", "sc-2"}
    assert len(grouped["sc-1"]) == 2
    assert len(grouped["sc-2"]) == 1
    # Within each scene, newest first
    assert grouped["sc-1"][0].version > grouped["sc-1"][1].version


def test_list_by_run_empty(service: VisualAssetService) -> None:
    grouped = run(service.list_by_run(run_id=999))
    assert grouped == {}


def test_list_by_run_excludes_other_runs(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/r1.png"))
    run(service.create_asset(run_id=2, scene_id="sc-1", asset_path="/r2.png"))

    grouped = run(service.list_by_run(run_id=1))
    assert len(grouped) == 1
    assert all(a.run_id == 1 for a in grouped["sc-1"])


# -- get_active_asset -----------------------------------------------------------


def test_get_active_asset(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v1.png"))
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v2.png"))

    active = run(service.get_active_asset(run_id=1, scene_id="sc-1"))

    assert active is not None
    assert active.version == 2  # Last created with is_active=True
    assert active.is_active is True


def test_get_active_asset_none(service: VisualAssetService) -> None:
    assert run(service.get_active_asset(run_id=999, scene_id="sc-1")) is None


# -- get_asset -------------------------------------------------------------------


def test_get_asset(service: VisualAssetService) -> None:
    created = run(
        service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v1.png")
    )

    fetched = run(service.get_asset(created.id))

    assert fetched is not None
    assert fetched.id == created.id
    assert fetched.asset_path == "/v1.png"


def test_get_asset_not_found(service: VisualAssetService) -> None:
    assert run(service.get_asset(999)) is None


# -- Concurrent saves -----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_saves_produce_unique_versions() -> None:
    """Two concurrent saves for the same scene must produce distinct versions."""
    storage = InMemoryVisualAssetStorage()
    svc = VisualAssetService(storage)

    results = await asyncio.gather(
        svc.create_asset(run_id=1, scene_id="sc-1", asset_path="/a.png"),
        svc.create_asset(run_id=1, scene_id="sc-1", asset_path="/b.png"),
    )

    versions = sorted(a.version for a in results)
    assert versions == [1, 2], f"Expected unique versions [1, 2], got {versions}"


@pytest.mark.asyncio
async def test_concurrent_saves_across_service_instances() -> None:
    """Two service instances sharing storage must still produce unique versions."""
    storage = InMemoryVisualAssetStorage()
    service_a = VisualAssetService(storage)
    service_b = VisualAssetService(storage)

    results = await asyncio.gather(
        service_a.create_asset(run_id=1, scene_id="sc-1", asset_path="/a.png"),
        service_b.create_asset(run_id=1, scene_id="sc-1", asset_path="/b.png"),
    )

    versions = sorted(a.version for a in results)
    assert versions == [1, 2], f"Expected unique versions [1, 2], got {versions}"


# -- Isolation -------------------------------------------------------------------


def test_different_runs_have_independent_assets(service: VisualAssetService) -> None:
    run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/r1.png"))
    run(service.create_asset(run_id=2, scene_id="sc-1", asset_path="/r2.png"))

    r1_assets = run(service.list_by_scene(run_id=1, scene_id="sc-1"))
    r2_assets = run(service.list_by_scene(run_id=2, scene_id="sc-1"))

    assert len(r1_assets) == 1
    assert len(r2_assets) == 1
    assert r1_assets[0].version == 1
    assert r2_assets[0].version == 1


# -- Concurrent active invariant (Oracle PR #140 feedback) ---------------------


@pytest.mark.asyncio
async def test_concurrent_creates_exactly_one_active() -> None:
    """Two concurrent active creates for the same scene must leave exactly one
    active asset, and it must be the latest version."""
    storage = InMemoryVisualAssetStorage()
    svc = VisualAssetService(storage)

    results = await asyncio.gather(
        svc.create_asset(run_id=1, scene_id="sc-1", asset_path="/a.png"),
        svc.create_asset(run_id=1, scene_id="sc-1", asset_path="/b.png"),
    )

    # Both should have unique versions
    versions = sorted(a.version for a in results)
    assert versions == [1, 2]

    # Exactly one active asset
    all_assets = await storage.list_assets_by_scene(1, "sc-1")
    active_assets = [a for a in all_assets if a.get("is_active", False)]
    assert len(active_assets) == 1, f"Expected 1 active, got {len(active_assets)}"

    # The active one must be the latest version
    assert active_assets[0]["version"] == 2


@pytest.mark.asyncio
async def test_concurrent_creates_across_instances_exactly_one_active() -> None:
    """Two service instances sharing storage: exactly one active after concurrent creates."""
    storage = InMemoryVisualAssetStorage()
    svc_a = VisualAssetService(storage)
    svc_b = VisualAssetService(storage)

    await asyncio.gather(
        svc_a.create_asset(run_id=1, scene_id="sc-1", asset_path="/a.png"),
        svc_b.create_asset(run_id=1, scene_id="sc-1", asset_path="/b.png"),
    )

    all_assets = await storage.list_assets_by_scene(1, "sc-1")
    active_assets = [a for a in all_assets if a.get("is_active", False)]
    assert len(active_assets) == 1
    assert active_assets[0]["version"] == 2


def test_create_inactive_preserves_existing_active(service: VisualAssetService) -> None:
    """Creating an asset with is_active=False must not deactivate the existing active asset."""
    first = run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v1.png", is_active=True))
    assert first.is_active is True

    second = run(service.create_asset(run_id=1, scene_id="sc-1", asset_path="/v2.png", is_active=False))
    assert second.is_active is False

    # First should still be active
    active = run(service.get_active_asset(run_id=1, scene_id="sc-1"))
    assert active is not None
    assert active.id == first.id
    assert active.is_active is True
