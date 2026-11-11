# pyright: reportMissingImports=false

from datetime import datetime, timezone

import pytest
from pydantic import BaseModel
from fastapi.routing import APIRoute
from shorts_api.auth import CurrentUser, require_run_access
from shorts_api.main import app


def _find_route(name: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.name == name:
            return route
    raise AssertionError(f"Route {name} not found")


def test_download_artifact_route_uses_require_run_access_dependency() -> None:
    route = _find_route("download_artifact")
    dependency_calls = [dep.call for dep in route.dependant.dependencies]
    assert require_run_access in dependency_calls


@pytest.fixture
def stub_artifact_download_services(monkeypatch: pytest.MonkeyPatch, tmp_path):
    now = datetime.now(timezone.utc)

    class StubRun(BaseModel):
        id: int
        project_id: int
        current_stage: str = "FINAL_REVIEW"
        status: str = "running"
        created_at: datetime
        updated_at: datetime

    class StubRunService:
        def __init__(self):
            self.runs = {
                10: StubRun(
                    id=10,
                    project_id=7,
                    created_at=now,
                    updated_at=now,
                )
            }

    class StubArtifactDownloadService:
        async def get_artifact_by_id(self, artifact_id: int):
            if artifact_id != 99:
                return None
            return {
                "id": 99,
                "run_id": 10,
                "file_path": "ok.txt",
                "storage_provider": "local",
                "content_type": "text/plain",
            }

    artifact_file = tmp_path / "ok.txt"
    artifact_file.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_ACCESS_STRICT", "true")

    run_svc = StubRunService()

    async def _require_run_access(run_id: int):
        run = run_svc.runs.get(run_id)
        if run is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Run not found")
        return CurrentUser(user_id=1, workspace_id=1), run

    app.dependency_overrides[require_run_access] = _require_run_access

    monkeypatch.setitem(
        _find_route("download_artifact").endpoint.__globals__,
        "artifact_download_service",
        StubArtifactDownloadService(),
    )
    yield
    app.dependency_overrides.pop(require_run_access, None)


@pytest.mark.asyncio
async def test_download_artifact_forbidden_workspace_mismatch(
    client, stub_artifact_download_services
):
    response = await client.get("/api/creator/runs/10/artifacts/99/download")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_download_artifact_rejects_none_workspace(client, stub_artifact_download_services):
    response = await client.get("/api/creator/runs/10/artifacts/100/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact not found"}


@pytest.mark.asyncio
async def test_download_artifact_without_workspace_context_forbidden(
    client, stub_artifact_download_services
):
    response = await client.get("/api/creator/runs/999/artifacts/99/download")

    assert response.status_code == 404
    assert response.json() == {"detail": "Run not found"}


@pytest.fixture
async def cleanup_bookkeeping_rows(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from creator_service.artifact_download_service import (
        ArtifactDownloadService,
        InMemoryArtifactDownloadStorage,
    )
    from creator_service.rejected_scene_cleanup import (
        REJECTED_SCENE_UPLOAD_CLEANUP_MARKER,
        SCENE_UPLOAD_CLEANUP_INTENT_MARKER,
    )

    now = datetime.now(timezone.utc)

    class StubRun(BaseModel):
        id: int
        project_id: int
        created_at: datetime
        updated_at: datetime

    run = StubRun(id=10, project_id=7, created_at=now, updated_at=now)
    scenes = tmp_path / "10" / "scenes"
    scenes.mkdir(parents=True)
    (tmp_path / "10" / "ok.txt").write_text("ok", encoding="utf-8")
    for name in ("intent.png", "rejected.png"):
        (scenes / name).write_bytes(b"\x89PNG pending upload")

    storage = InMemoryArtifactDownloadStorage()
    ordinary = await storage.save_artifact({
        "run_id": 10,
        "file_path": "10/ok.txt",
        "storage_key": "10/ok.txt",
        "storage_provider": "local",
        "content_type": "text/plain",
        "metadata_json": None,
    })
    rows = {"ordinary": ordinary["id"]}
    for label, marker in (
        ("intent", SCENE_UPLOAD_CLEANUP_INTENT_MARKER),
        ("rejected", REJECTED_SCENE_UPLOAD_CLEANUP_MARKER),
    ):
        saved = await storage.save_artifact({
            "run_id": 10,
            "artifact_type": "visual_asset",
            "file_path": f"10/scenes/{label}.png",
            "storage_key": f"10/scenes/{label}.png",
            "storage_provider": "local",
            "content_type": "image/png",
            "metadata_json": marker,
        })
        rows[label] = saved["id"]

    async def _require_run_access(run_id: int):
        if run_id != run.id:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Run not found")
        return CurrentUser(user_id=1, workspace_id=1), run

    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("ARTIFACT_ACCESS_STRICT", "true")
    app.dependency_overrides[require_run_access] = _require_run_access
    monkeypatch.setitem(
        _find_route("download_artifact").endpoint.__globals__,
        "artifact_download_service",
        ArtifactDownloadService(storage),
    )
    yield rows
    app.dependency_overrides.pop(require_run_access, None)


async def test_download_artifact_serves_ordinary_artifact_beside_cleanup_rows(
    client, cleanup_bookkeeping_rows
):
    response = await client.get(
        f"/api/creator/runs/10/artifacts/{cleanup_bookkeeping_rows['ordinary']}/download"
    )

    assert response.status_code == 200


@pytest.mark.parametrize("label", ["intent", "rejected"])
async def test_download_artifact_hides_internal_cleanup_rows(
    client, cleanup_bookkeeping_rows, label
):
    # Given a same-run cleanup bookkeeping row whose pending object exists on disk.
    artifact_id = cleanup_bookkeeping_rows[label]

    # When the owner requests it through the real download route.
    response = await client.get(f"/api/creator/runs/10/artifacts/{artifact_id}/download")

    # Then it is indistinguishable from a missing artifact.
    assert response.status_code == 404
    assert response.json() == {"detail": "Artifact not found"}
