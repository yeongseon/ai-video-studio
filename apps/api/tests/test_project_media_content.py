from datetime import datetime, timezone
from pathlib import Path

from creator_domain.models import MediaAsset, MediaOrigin, MediaType, Project
from creator_service.media_asset_service import InMemoryMediaAssetStorage, MediaAssetService
from creator_service.object_storage import LocalStorageBackend
from fastapi import HTTPException
from httpx import AsyncClient
import pytest

from shorts_api.auth import CurrentUser, require_project_access
from shorts_api.main import app


@pytest.mark.asyncio
async def test_project_content_requires_owned_asset_and_serves_real_png(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from shorts_api.routes import creator_timeline

    backend = LocalStorageBackend(str(tmp_path))
    storage = InMemoryMediaAssetStorage()
    service = MediaAssetService(storage_backend=backend, asset_storage=storage)
    data = b"\x89PNG\r\n\x1a\nreview"
    key = "workspaces/7/assets/review.png"
    backend.upload(key, data, content_type="image/png")
    asset = MediaAsset(
        id=1, workspace_id=7, project_id=42, media_type=MediaType.IMAGE,
        origin=MediaOrigin.GENERATED, storage_key=key, mime_type="image/png",
        metadata={"storage_provider": "local"}, created_at=datetime.now(timezone.utc),
    )
    saved = await storage.save_asset(asset.model_dump(exclude={"id"}))
    monkeypatch.setattr(creator_timeline, "media_asset_service", service)
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr("creator_service.object_storage._storage_backend", backend)

    async def access(project_id: int) -> tuple[CurrentUser, Project]:
        if project_id == 99:
            raise HTTPException(404, "Project not found")
        return CurrentUser(user_id=1, workspace_id=7), Project(
            id=project_id, workspace_id=7, created_at=asset.created_at, updated_at=asset.created_at,
        )

    app.dependency_overrides[require_project_access] = access
    try:
        response = await client.get(f"/api/creator/projects/42/assets/{saved['id']}/content")
        assert response.status_code == 200, response.text
        assert response.content == data
        assert response.headers["content-type"].startswith("image/png")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(f"/api/creator/projects/43/assets/{saved['id']}/content")).status_code == 404
        assert (await client.get(f"/api/creator/projects/99/assets/{saved['id']}/content")).status_code == 404
        assert (await client.get("/api/creator/projects/42/assets/9999/content")).status_code == 404
    finally:
        app.dependency_overrides.pop(require_project_access, None)
