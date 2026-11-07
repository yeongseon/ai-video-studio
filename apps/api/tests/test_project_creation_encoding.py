import json

import pytest
from httpx import AsyncClient

from tests.test_project_settings_api import settings_service
from creator_service.project_service import ProjectService
from shorts_api.schemas.creator_projects import CreateProjectRequest
from shorts_api.routes.project_settings_boundary import JsonDepthValidator


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["a", "한", "😀", "é", '"', "\\", " "])
@pytest.mark.parametrize("explicit", [False, True])
async def test_creation_when_max_source_escaped_preserves_content(
    client: AsyncClient, settings_service: ProjectService, character: str, explicit: bool,
) -> None:
    # Given a maximum-length legitimate source encoded using standard JSON escaping.
    source = character * 200_000
    payload = {"title": "Maximum", "source_type": "pasted_json", "json_script": source}
    if explicit:
        payload["settings"] = {}
    # When creating through HTTP, then retain exactly the decoded source.
    response = await client.post("/api/creator/projects", content=json.dumps(payload),
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 201
    assert response.json()["json_script"] == source
    restored = await settings_service.get_project(response.json()["id"], workspace_id=1)
    assert restored is not None and restored.json_script == source


@pytest.mark.asyncio
@pytest.mark.parametrize("field,source_type,maximum", [
    ("idea_brief", "idea", 5000), ("markdown_source", "markdown", 50000),
    ("json_script", "pasted_json", 200000),
])
async def test_creation_when_source_exceeds_character_limit_rejected(
    client: AsyncClient, settings_service: ProjectService, field: str, source_type: str, maximum: int,
) -> None:
    # Given a source one character beyond the existing schema maximum.
    payload = {"title": "Too long", "source_type": source_type, field: "😀" * (maximum + 1)}
    # When validating, then preserve the field-level length error, not a byte-budget error.
    response = await client.post("/api/creator/projects", content=json.dumps(payload),
                                 headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", field]
    assert response.json()["detail"][0]["type"] == "string_too_long"


@pytest.mark.parametrize("explicit", [False, True])
def test_creation_schema_when_all_fields_at_character_maxima(explicit: bool) -> None:
    # Given all individually valid source maxima; source exclusivity belongs to the service.
    payload = {"title": "😀" * 200, "source_type": "pasted_json", "idea_brief": "😀" * 5000,
               "markdown_source": "😀" * 50000, "json_script": "😀" * 200000, "url_source": "😀" * 2000}
    if explicit:
        payload["settings"] = {}
    # When parsing the maximum escaped request, then every field remains schema-valid.
    parsed = CreateProjectRequest.model_validate_json(json.dumps(payload))
    assert parsed.json_script == payload["json_script"]


@pytest.mark.asyncio
async def test_creation_when_padding_stream_rejected_early(client: AsyncClient, settings_service: ProjectService) -> None:
    # Given a padding stream that must stop before consuming its full million bytes.
    from collections.abc import AsyncIterator
    from unittest.mock import AsyncMock
    consumed = 0
    spy = AsyncMock(wraps=settings_service.db.insert_project)
    settings_service.db.insert_project = spy
    async def chunks() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(1000):
            consumed += 1024
            yield b" " * 1024
        yield b'{"title":"New","source_type":"idea","idea_brief":"idea"}'
    # When streaming with a lying length, then reject before persistence and stop consumption.
    response = await client.post("/api/creator/projects", content=chunks(),
                                 headers={"Content-Type": "application/json", "Content-Length": "1"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "settings_whitespace"
    assert consumed <= 66 * 1024
    spy.assert_not_called()


def test_whitespace_when_inside_string_is_content() -> None:
    # Given a million spaces within a quoted value, when lexing resource boundaries.
    validator = JsonDepthValidator()
    validator.feed(b'{"content":"' + b" " * 1_000_000 + b'"}')
    # Then content whitespace is not counted as insignificant padding.
    assert validator.whitespace == 0


@pytest.mark.parametrize("field,maximum", [("idea_brief", 5000), ("markdown_source", 50000), ("json_script", 200000)])
def test_creation_schema_when_content_spaces_at_maximum(field: str, maximum: int) -> None:
    # Given string whitespace within existing character maxima, when validating it.
    parsed = CreateProjectRequest.model_validate({"title": "Whitespace", "source_type": "pasted_json", field: " " * maximum})
    # Then preserve the full content length rather than treating it as padding.
    assert len(getattr(parsed, field)) == maximum
