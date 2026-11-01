from __future__ import annotations

from typing import Literal

from creator_domain.models.project import Project
from creator_domain.models.project_settings import ProjectSettings

from .project_storage import InMemoryProjectStorage, LatestRunSummary, ProjectStorageBackend


class ProjectWithLatestRun(Project):
    latest_run: LatestRunSummary | None = None


class ProjectService:
    _ALLOWED_SOURCE_TYPES = {"idea", "markdown", "url", "pasted_json"}

    def __init__(self, db: ProjectStorageBackend | None = None) -> None:
        self.db = db if db is not None else InMemoryProjectStorage()

    async def create_project(
        self,
        title: str,
        source_type: Literal["idea", "markdown", "url", "pasted_json"],
        idea_brief: str | None = None,
        markdown_source: str | None = None,
        url_source: str | None = None,
        json_script: str | None = None,
        workspace_id: int | None = None,
        settings: ProjectSettings | None = None,
    ) -> Project:
        if source_type not in self._ALLOWED_SOURCE_TYPES:
            raise ValueError(f"Unsupported source_type '{source_type}'")

        if source_type == "markdown" and markdown_source is None:
            raise ValueError("source_type='markdown' requires markdown_source to be provided")
        if source_type == "url" and url_source is None:
            raise ValueError("source_type='url' requires url_source to be provided")
        if source_type == "idea" and idea_brief is None:
            raise ValueError("source_type='idea' requires idea_brief to be provided")

        if source_type == "idea" and (markdown_source is not None or url_source is not None):
            raise ValueError("source_type='idea' cannot have markdown_source or url_source set")
        if source_type == "markdown" and (idea_brief is not None or url_source is not None):
            raise ValueError("source_type='markdown' cannot have idea_brief or url_source set")
        if source_type == "url" and (idea_brief is not None or markdown_source is not None):
            raise ValueError("source_type='url' cannot have idea_brief or markdown_source set")

        if source_type == "pasted_json" and json_script is None:
            raise ValueError("source_type='pasted_json' requires json_script to be provided")
        if source_type == "pasted_json" and (
            idea_brief is not None or markdown_source is not None or url_source is not None
        ):
            raise ValueError(
                "source_type='pasted_json' cannot have idea_brief, markdown_source, or url_source set"
            )
        if source_type != "pasted_json" and json_script is not None:
            raise ValueError(f"source_type='{source_type}' cannot have json_script set")

        row = await self.db.insert_project(
            {
                "title": title,
                "source_type": source_type,
                "idea_brief": idea_brief,
                "markdown_source": markdown_source,
                "url_source": url_source,
                "json_script": json_script,
                "status": "draft",
                "workspace_id": workspace_id,
                "settings": settings.model_dump(mode="json") if settings is not None else None,
            }
        )
        return Project.model_validate(row)

    async def get_project(self, project_id: int, workspace_id: int | None = None) -> Project | None:
        row = await self.db.fetch_project(project_id, workspace_id=workspace_id)
        if row is None:
            return None

        latest_run = await self.db.fetch_latest_run_summary(project_id)
        return ProjectWithLatestRun.model_validate({**row, "latest_run": latest_run})

    async def list_projects(
        self, limit: int = 20, offset: int = 0, workspace_id: int | None = None
    ) -> list[Project]:
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if offset < 0:
            raise ValueError("offset must be >= 0")

        rows = await self.db.list_projects_with_latest_run(
            limit=limit, offset=offset, workspace_id=workspace_id,
        )
        return [ProjectWithLatestRun.model_validate(row) for row in rows]

    async def count_projects(self, workspace_id: int | None = None) -> int:
        return await self.db.count_projects(workspace_id=workspace_id)

    async def update_project(
        self, project_id: int, title: str, workspace_id: int | None = None
    ) -> Project | None:
        row = await self.db.update_project(project_id, {"title": title}, workspace_id=workspace_id)
        if row is None:
            return None
        latest_run = await self.db.fetch_latest_run_summary(project_id)
        return ProjectWithLatestRun.model_validate({**row, "latest_run": latest_run})

    async def delete_project(self, project_id: int, workspace_id: int | None = None) -> bool:
        return await self.db.delete_project(project_id, workspace_id=workspace_id)

    async def replace_settings(
        self, project: Project, settings: ProjectSettings, *, workspace_id: int,
    ) -> Project | None:
        row = await self.db.update_project(
            project.id, {"settings": settings.model_dump(mode="json")},
            workspace_id=workspace_id, expected_updated_at=project.updated_at,
        )
        return Project.model_validate(row) if row is not None else None

    async def mark_deleting(self, project_id: int, workspace_id: int) -> Project:
        row = await self.db.update_project(
            project_id, {"status": "deleting"}, workspace_id=workspace_id,
        )
        if row is None:
            raise ValueError(f"Project {project_id} not found")
        return Project.model_validate(row)


def _create_storage() -> ProjectStorageBackend:
    import os

    if os.getenv("DATABASE_URL"):
        from .postgres_project_storage import PostgresProjectStorage

        return PostgresProjectStorage()
    return InMemoryProjectStorage()


project_service = ProjectService(_create_storage())
