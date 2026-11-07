import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from creator_service.artifact_download_service import (
    ArtifactDownloadService,
    InMemoryArtifactDownloadStorage,
)
from creator_service.rejected_scene_cleanup import SCENE_UPLOAD_CLEANUP_INTENT_MARKER
from creator_service.visual_asset_service import (
    InMemoryVisualAssetStorage,
    SceneAssetAcceptanceRejected,
)


class AcceptedSceneConnection:
    def __init__(self, row: dict[str, str | int]) -> None:
        self.row = row
        self.claimed = False
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *args: object) -> dict[str, str | int] | None:
        self.queries.append((query, args))
        if "FOR UPDATE SKIP LOCKED" in query and not self.claimed:
            self.claimed = True
            return self.row
        return None

    async def fetchval(self, query: str, *args: object) -> int:
        self.queries.append((query, args))
        return 601 if "FROM creator_scene_assets" in query else 0

    async def execute(self, query: str, *args: object) -> str:
        self.queries.append((query, args))
        return "DELETE 1"


@contextlib.asynccontextmanager
async def transaction(connection: AcceptedSceneConnection) -> AsyncIterator[AcceptedSceneConnection]:
    yield connection


@pytest.mark.asyncio
async def test_retry_stale_intent_preserves_accepted_scene_in_memory_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given an accepted scene reference shares an exact key with a stale intent.
    local_path = tmp_path / "accepted.png"
    local_path.write_bytes(b"accepted")
    connection = AcceptedSceneConnection({
        "id": 600,
        "run_id": 17,
        "storage_key": "17/scenes/accepted.png",
        "storage_provider": "local",
        "file_path": str(local_path),
        "metadata_json": SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
        "delete_retry_count": 0,
    })
    deleted_keys: list[str] = []

    class Backend:
        def delete(self, candidate: str) -> bool:
            deleted_keys.append(candidate)
            return True

    monkeypatch.setattr(
        "creator_service.artifact_download_service.transaction",
        lambda: transaction(connection),
    )
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: Backend(),
    )

    # When retry processes the locked stale intent.
    result = await ArtifactDownloadService(
        InMemoryArtifactDownloadStorage(),
    ).retry_failed_deletions()

    # Then only the intent row is removed, never accepted bytes.
    assert result == 1
    assert deleted_keys == []
    assert local_path.read_bytes() == b"accepted"
    assert any("DELETE FROM creator_artifacts" in query for query, _args in connection.queries)


@pytest.mark.asyncio
async def test_intent_backed_in_memory_acceptance_rejects_lost_ownership() -> None:
    # Given an upload whose cleanup intent was already claimed by retry.
    async def accept_run(_run_id: int) -> bool:
        return True

    async def claim_intent(_intent_id: int, _storage_key: str) -> bool:
        return False

    storage = InMemoryVisualAssetStorage(
        acceptance_guard=accept_run,
        upload_intent_claim=claim_intent,
    )

    # When intent-backed acceptance attempts to save the uploaded key.
    with pytest.raises(SceneAssetAcceptanceRejected):
        await storage.save_asset({
            "run_id": 17,
            "scene_id": "lost-intent",
            "asset_path": "/tmp/lost-intent.png",
            "storage_key": "17/scenes/lost-intent.png",
            "upload_intent_id": 701,
        })

    # Then no dangling scene version is recorded.
    assert await storage.list_assets_by_run(17) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["local", "s3"])
async def test_project_run_deletion_cleans_intent_exact_key_and_local_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
) -> None:
    # Given an upload intent, its exact object, local source, and an unrelated alias.
    key = "17/scenes/fresh.png"
    alias = "17/scenes/shared.png"
    local_path = tmp_path / "local-source" / "fresh.png"
    local_path.parent.mkdir()
    local_path.write_bytes(b"fresh-local")
    deleted_keys: list[str] = []

    class Backend:
        def delete(self, candidate: str) -> bool:
            deleted_keys.append(candidate)
            return True

    async def fetch_rows(_query: str, *_args: object) -> list[dict[str, str | int]]:
        return [{
            "id": 507,
            "storage_key": key,
            "file_path": str(local_path),
            "metadata_json": SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
            "object_storage_provider": provider,
        }]

    async def execute(_query: str, *_args: object) -> str:
        return "OK"

    monkeypatch.setenv("STORAGE_BACKEND", provider)
    monkeypatch.setattr("creator_service.artifact_download_service.fetch_all", fetch_rows)
    monkeypatch.setattr("creator_service.artifact_download_service.execute", execute)
    monkeypatch.setattr(
        "creator_service.artifact_download_service.get_storage_backend",
        lambda: Backend(),
    )
    service = ArtifactDownloadService(InMemoryArtifactDownloadStorage())

    # When project cleanup is retried after the first successful pass.
    first = await service.delete_artifacts_for_run(17)
    second = await service.delete_artifacts_for_run(17)

    # Then only the recorded key is targeted and local cleanup is idempotent.
    assert first == 0
    assert second == 0
    assert deleted_keys == [key, key]
    assert alias not in deleted_keys
    assert not local_path.exists()
