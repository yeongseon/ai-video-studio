"""Owned assets and a counted, bounded remote adapter for preview contracts."""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from creator_domain.models import MediaAsset, MediaOrigin, MediaType, Project
from creator_service.media_asset_service import InMemoryMediaAssetStorage, MediaAssetService
from creator_service.object_storage import LocalStorageBackend
from fastapi import HTTPException
import pytest

from shorts_api.auth import CurrentUser, require_project_access
from shorts_api.main import app


class CountedStorage(LocalStorageBackend):
    """Remote bytes are separate from the local collision; counters expose I/O."""

    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.provider = "s3"
        self.data = b"remote-video-0123456789"
        self.calls = 0
        self.reads: list[tuple[int, int]] = []
        self.failure: Exception | None = None
        self.size = len(self.data)
        self.active = 0

    def stat(self, key: str):
        from creator_service.object_storage import ObjectInfo

        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ObjectInfo(size=self.size)

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        self.calls += 1
        self.reads.append((offset, length))
        assert 0 < length <= 64 * 1024, "whole-object buffering attempted"
        self.active += 1
        try:
            if self.failure is not None:
                raise self.failure
            if self.size > len(self.data):
                return b"x" * length
            return self.data[offset:offset + length]
        finally:
            self.active -= 1

    def download_bytes(self, key: str) -> bytes:
        raise AssertionError("preview must not use whole-object download")


class PreviewFixture:
    """Mutable fixture owns asset setup and dependency cleanup."""

    def __init__(self, backend: CountedStorage, storage: InMemoryMediaAssetStorage) -> None:
        self.backend = backend
        self.storage = storage

    async def asset(self, *, workspace: int = 7, project: int = 42,
                    provider: str = "s3", mime: str = "video/mp4",
                    key: str = "workspaces/7/assets/private.mp4") -> int:
        asset = MediaAsset(
            id=1, workspace_id=workspace, project_id=project, media_type=MediaType.VIDEO,
            origin=MediaOrigin.GENERATED, storage_key=key, mime_type=mime,
            metadata={"storage_provider": provider}, created_at=datetime.now(timezone.utc),
        )
        saved = await self.storage.save_asset(asset.model_dump(exclude={"id"}))
        return int(saved["id"])


@pytest.fixture
async def preview(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[PreviewFixture]:
    from shorts_api.routes import creator_timeline
    import creator_service.object_storage as objects

    backend = CountedStorage(tmp_path)
    storage = InMemoryMediaAssetStorage()
    service = MediaAssetService(storage_backend=backend, asset_storage=storage)
    monkeypatch.setattr(creator_timeline, "media_asset_service", service)
    monkeypatch.setattr(objects, "_storage_backend", backend)
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    local = tmp_path / "workspaces/7/assets/private.mp4"
    local.parent.mkdir(parents=True)
    local.write_bytes(b"different-local-collision")

    async def access(project_id: int) -> tuple[CurrentUser, Project]:
        if project_id == 99:
            raise HTTPException(404, "Project not found")
        now = datetime.now(timezone.utc)
        return CurrentUser(user_id=1, workspace_id=7), Project(
            id=project_id, workspace_id=7, created_at=now, updated_at=now,
        )

    app.dependency_overrides[require_project_access] = access
    try:
        yield PreviewFixture(backend, storage)
    finally:
        app.dependency_overrides.pop(require_project_access, None)


def content_url(asset: int, project: int = 42) -> str:
    return f"/api/creator/projects/{project}/assets/{asset}/content"
