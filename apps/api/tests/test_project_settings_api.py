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


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,budget", [("POST", "/api/creator/projects", 1_000_000),
                                               ("PUT", "/api/creator/projects/1/settings", 5000)])
async def test_streamed_body_when_oversized_never_writes(client: AsyncClient, settings_service: ProjectService,
                                                       monkeypatch: pytest.MonkeyPatch, method: str, path: str, budget: int) -> None:
    # Given chunked input with no Content-Length and storage-write spies.
    insert = AsyncMock(wraps=settings_service.db.insert_project)
    update = AsyncMock(wraps=settings_service.db.update_project)
    monkeypatch.setattr(settings_service.db, "insert_project", insert)
    monkeypatch.setattr(settings_service.db, "update_project", update)
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(budget // 1000):
            yield b" " * 1000
        yield b'{"title":"New","source_type":"idea","idea_brief":"idea","settings":{}}' if method == "POST" else b"{}"
    # When consuming the request, then reject before any persistence.
    response = await client.request(method, path, content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "body"
    insert.assert_not_called()
    update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [("POST", "/api/creator/projects"), ("PUT", "/api/creator/projects/1/settings")])
async def test_settings_depth_when_nested_rejected_before_writes(client: AsyncClient, settings_service: ProjectService,
                                                               monkeypatch: pytest.MonkeyPatch, method: str, path: str) -> None:
    # Given a depth bomb with a small byte size.
    spy = AsyncMock(wraps=settings_service.db.insert_project)
    update = AsyncMock(wraps=settings_service.db.update_project)
    monkeypatch.setattr(settings_service.db, "insert_project", spy)
    monkeypatch.setattr(settings_service.db, "update_project", update)
    nested = '{"output_preset":' + '[' * 1000 + '0' + ']' * 1000 + '}'
    body = '{"title":"New","source_type":"idea","idea_brief":"idea","settings":' + nested + '}' if method == "POST" else nested
    # When parsing, then report a field-level depth error, never parser recursion failure.
    response = await client.request(method, path, content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "settings_depth"
    spy.assert_not_called()
    update.assert_not_called()


@pytest.mark.asyncio
async def test_creation_when_large_source_remains_supported(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given a near-limit JSON source containing structural-looking text.
    source = "[\\\"{}]" * 28000
    # When creating with supported settings, then preserve the existing source budget.
    response = await client.post("/api/creator/projects", json={"title": "Large", "source_type": "pasted_json",
                                "json_script": source, "settings": {}})
    assert response.status_code == 201
    assert response.json()["json_script"] == source


@pytest.mark.asyncio
async def test_read_when_schema_not_migrated_is_sanitized(client: AsyncClient, settings_service: ProjectService,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncpg
    # Given a missing settings table, when a new reader handles a legacy database.
    monkeypatch.setattr(settings_service.db, "fetch_settings", AsyncMock(side_effect=asyncpg.UndefinedTableError("SELECT secret_sql")))
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
                           headers=client.headers) as external:
        response = await external.get("/api/creator/projects/1")
    # Then return a sanitized deployment failure without database internals.
    assert response.status_code == 500
    assert "secret_sql" not in response.text


@pytest.mark.asyncio
async def test_detail_and_list_when_settings_are_workspace_scoped(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given distinct explicit settings in two workspaces.
    from creator_domain.models.project_settings import ProjectSettings
    first = await settings_service.get_project(1, 1)
    second = await settings_service.get_project(2, 2)
    assert first is not None and second is not None
    await settings_service.replace_settings(first, ProjectSettings(output_preset="short_square"), workspace_id=1)
    await settings_service.replace_settings(second, ProjectSettings(output_preset="short_landscape"), workspace_id=2)
    # When reading owned detail/list, then no foreign setting is exposed.
    detail = await client.get("/api/creator/projects/1")
    listing = await client.get("/api/creator/projects")
    foreign = await client.get("/api/creator/projects/2")
    assert detail.json()["settings"]["output_preset"] == "short_square"
    assert [p["id"] for p in listing.json()["projects"]] == [1]
    assert listing.json()["projects"][0]["settings"]["output_preset"] == "short_square"
    assert foreign.status_code == 404
