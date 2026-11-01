from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from creator_service.project_service import InMemoryProjectStorage, ProjectService
from shorts_api import auth
from shorts_api.main import app
from shorts_api.routes import creator_projects


@pytest.fixture
async def settings_service(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ProjectService]:
    service = ProjectService(InMemoryProjectStorage())
    await service.create_project("Owned", "idea", idea_brief="owned", workspace_id=1)
    await service.create_project("Foreign", "idea", idea_brief="foreign", workspace_id=2)
    monkeypatch.setattr(creator_projects, "project_service", service)
    monkeypatch.setattr("creator_service.project_service.project_service", service)
    monkeypatch.setattr(auth.workspace_service, "check_access", AsyncMock(return_value=True))
    yield service


@pytest.mark.asyncio
async def test_creation_when_settings_are_explicit(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given non-default settings, when creating a project through HTTP.
    response = await client.post("/api/creator/projects", json={
        "title": "Square", "source_type": "idea", "idea_brief": "idea",
        "settings": {"output_preset": "short_square", "encoding_profile": "high_quality",
                     "target_duration_seconds": 240},
    })
    # Then persist and expose the typed effective contract.
    assert response.status_code == 201
    assert response.json()["settings_source"] == "explicit"
    assert response.json()["settings"]["output_preset"] == "short_square"


@pytest.mark.asyncio
async def test_read_when_project_is_legacy(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given a project without settings, when reading through HTTP.
    response = await client.get("/api/creator/projects/1")
    # Then identify the historical default without backfilling storage.
    assert response.status_code == 200
    assert response.json()["settings_source"] == "legacy_default"
    assert response.json()["settings"]["encoding_profile"] == "legacy_default"


@pytest.mark.asyncio
async def test_replace_when_owned(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given an owned project, when replacing settings.
    response = await client.put("/api/creator/projects/1/settings", json={"output_preset": "short_square"})
    # Then return the persisted replacement.
    assert response.status_code == 200
    assert response.json()["settings_source"] == "explicit"
    assert response.json()["settings"]["output_preset"] == "short_square"
    restored = await settings_service.get_project(1, workspace_id=1)
    assert restored is not None
    assert restored.model_dump()["settings"]["output_preset"] == "short_square"


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", [2, 999])
async def test_replace_when_foreign_or_missing(client: AsyncClient, settings_service: ProjectService,
                                               monkeypatch: pytest.MonkeyPatch, project_id: int) -> None:
    # Given a foreign/missing project and a storage-write spy.
    spy = AsyncMock(wraps=settings_service.db.update_project)
    monkeypatch.setattr(settings_service.db, "update_project", spy)
    # When attempting replacement through the real ownership dependency.
    response = await client.put(f"/api/creator/projects/{project_id}/settings", json={})
    # Then hide existence and never write storage.
    assert response.status_code == 404
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_replace_when_unauthenticated(settings_service: ProjectService) -> None:
    # Given no credentials, when accessing the settings endpoint.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/api/creator/projects/1/settings", json={})
    # Then authentication is required.
    assert response.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"workspace_id": 2}, {"encoding_profile": "legacy_default"}, {"content_format": "long"},
    {"output_preset": "unknown"}, {"schema_version": 2},
    {"target_duration_seconds": True}, {"secret": "x" * 100000},
    {"output_preset": {"nested": {"nested": {"nested": "bad"}}}},
])
async def test_replace_when_invalid(client: AsyncClient, settings_service: ProjectService,
                                    payload: dict[str, object]) -> None:
    # Given invalid/reserved settings, when crossing the HTTP boundary.
    response = await client.put("/api/creator/projects/1/settings", json=payload)
    # Then return field-level errors rather than accepting mass assignment.
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"


@pytest.mark.asyncio
async def test_replace_when_body_exceeds_settings_budget(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given an oversized but otherwise valid settings body, when replacing settings.
    response = await client.put("/api/creator/projects/1/settings", content=" " * 5000 + "{}",
                                headers={"Content-Type": "application/json"})
    # Then enforce a settings-specific byte budget.
    assert response.status_code == 422
