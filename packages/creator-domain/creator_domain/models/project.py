from datetime import datetime
from typing import ClassVar
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from creator_domain.models.project_settings import ProjectSettings


class Project(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    workspace_id: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=255)
    source_type: Literal["idea", "markdown", "pasted_json", "url"] = "idea"
    idea_brief: str | None = None
    markdown_source: str | None = None
    url_source: str | None = None
    json_script: str | None = None
    settings: ProjectSettings | None = None
    status: Literal["draft", "active", "completed", "archived", "deleting"] = "draft"
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Project":
        return cls.model_validate(data)

    def to_json(self) -> str:
        return self.model_dump_json()
