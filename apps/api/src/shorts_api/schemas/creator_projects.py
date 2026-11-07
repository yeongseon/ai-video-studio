"""Schemas for project management operations."""

from __future__ import annotations

from typing import Literal

from creator_domain.models.project_settings import ProjectSettings, resolve_effective_settings
from pydantic import BaseModel, ConfigDict, Field


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=200)
    source_type: Literal["idea", "markdown", "pasted_json", "url"]
    idea_brief: str | None = Field(default=None, max_length=5000)
    markdown_source: str | None = Field(default=None, max_length=50000)
    json_script: str | None = Field(default=None, max_length=200000)
    url_source: str | None = Field(default=None, max_length=2000)
    settings: ProjectSettings | None = None


class UpdateProjectRequest(BaseModel):
    title: str = Field(max_length=200)


def project_response(project: BaseModel) -> dict[str, object]:
    data = project.model_dump(mode="json")
    stored = ProjectSettings.model_validate(data["settings"]) if data.get("settings") is not None else None
    effective = resolve_effective_settings(stored)
    return {**data, "settings": effective.settings.model_dump(mode="json"),
            "settings_source": effective.source}
