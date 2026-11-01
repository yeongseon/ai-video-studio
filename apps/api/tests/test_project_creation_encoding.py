import json

import pytest
from httpx import AsyncClient

from tests.test_project_settings_api import settings_service
from creator_service.project_service import ProjectService
from shorts_api.schemas.creator_projects import CreateProjectRequest


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
