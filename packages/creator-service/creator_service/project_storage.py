"""Project service with a pluggable async storage backend.

The long-term target is an async DB connection/pool (for example, asyncpg).
Until DB wiring is in place, this module uses an in-memory backend that exposes
the same async service-facing interface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import creator_service.run_service as _run_svc_mod

LatestRunSummary = dict[str, int | str | None]


class ProjectStorageBackend(Protocol):
    """Abstract async storage interface used by ProjectService."""

    async def insert_project(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def fetch_project(
        self, project_id: int, workspace_id: int | None = None
    ) -> dict[str, Any] | None: ...

    async def list_projects(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]: ...

    async def list_projects_with_latest_run(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]: ...

    async def fetch_latest_run_summary(self, project_id: int) -> LatestRunSummary | None: ...

    async def count_projects(self, workspace_id: int | None = None) -> int: ...

    async def update_project(
        self,
        project_id: int,
        updates: dict[str, Any],
        *,
        workspace_id: int | None = None,
        expected_updated_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Update project fields. Returns updated row or None if not found."""
        ...

    async def delete_project(self, project_id: int, *, workspace_id: int | None = None) -> bool:
        """Delete a project by id. Returns True if deleted."""
        ...


class InMemoryProjectStorage:
    """Temporary async storage backend used before DB integration."""

    def __init__(self) -> None:
        self._projects: dict[int, dict[str, Any]] = {}
        self._runs_by_project: dict[int, list[dict[str, Any]]] = {}
        self._next_project_id = 1
        self._next_run_id = 1

    async def insert_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        project_id = self._next_project_id
        self._next_project_id += 1

        row = {
            "id": project_id,
            "title": payload.get("title"),
            "source_type": payload.get("source_type", "idea"),
            "idea_brief": payload.get("idea_brief"),
            "markdown_source": payload.get("markdown_source"),
            "url_source": payload.get("url_source"),
            "json_script": payload.get("json_script"),
            "settings": payload.get("settings"),
            "status": payload.get("status", "draft"),
            "workspace_id": payload.get("workspace_id"),
            "created_at": now,
            "updated_at": now,
        }
        self._projects[project_id] = row
        return dict(row)

    async def fetch_project(
        self, project_id: int, workspace_id: int | None = None
    ) -> dict[str, Any] | None:
        row = self._projects.get(project_id)
        if row is None:
            return None
        if workspace_id is not None and row.get("workspace_id") != workspace_id:
            return None
        return dict(row)

    async def list_projects(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            self._projects.values(),
            key=lambda row: (row["created_at"], row["id"]),
            reverse=True,
        )
        if workspace_id is not None:
            ordered = [row for row in ordered if row.get("workspace_id") == workspace_id]
        return [dict(row) for row in ordered[offset : offset + limit]]

    async def list_projects_with_latest_run(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]:
        rows = await self.list_projects(limit=limit, offset=offset, workspace_id=workspace_id)
        enriched: list[dict[str, Any]] = []
        for row in rows:
            latest_run = await self.fetch_latest_run_summary(row["id"])
            enriched.append({**row, "latest_run": latest_run})
        return enriched

    async def count_projects(self, workspace_id: int | None = None) -> int:
        if workspace_id is None:
            return len(self._projects)
        return sum(1 for row in self._projects.values() if row.get("workspace_id") == workspace_id)

    async def update_project(
        self,
        project_id: int,
        updates: dict[str, Any],
        *,
        workspace_id: int | None = None,
        expected_updated_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        row = self._projects.get(project_id)
        if row is None:
            return None
        if workspace_id is not None and row.get("workspace_id") != workspace_id:
            return None
        if expected_updated_at is not None and row["updated_at"] != expected_updated_at:
            return None
        for key, value in updates.items():
            row[key] = value
        row["updated_at"] = datetime.now(timezone.utc)
        return dict(row)

    async def delete_project(self, project_id: int, *, workspace_id: int | None = None) -> bool:
        row = self._projects.get(project_id)
        if row is None:
            return False
        if workspace_id is not None and row.get("workspace_id") != workspace_id:
            return False
        if project_id in self._projects:
            del self._projects[project_id]
            # Also remove associated runs
            self._runs_by_project.pop(project_id, None)
            return True
        return False

    async def fetch_latest_run_summary(self, project_id: int) -> LatestRunSummary | None:
        runs = self._runs_by_project.get(project_id, [])
        if runs:
            latest = max(runs, key=lambda run: run["id"])
            return {
                "run_id": latest["id"],
                "current_stage": latest["current_stage"],
                "status": latest["status"],
            }

        # In real local/dev flow, runs are created through RunService, not via
        # this storage's insert_run helper.  Fall back to the shared in-memory
        # run service so latest_run stays accurate outside Postgres mode.
        try:
            persisted_runs = await _run_svc_mod.run_service.list_runs_by_project(project_id)
        except Exception:
            return None
        if not persisted_runs:
            return None
        latest_run = persisted_runs[0]
        return {
            "run_id": latest_run.id,
            "current_stage": latest_run.current_stage,
            "status": latest_run.status,
        }

    async def insert_run(
        self,
        project_id: int,
        current_stage: str | None,
        status: str,
    ) -> dict[str, Any]:
        run_id = self._next_run_id
        self._next_run_id += 1
        row = {
            "id": run_id,
            "project_id": project_id,
            "current_stage": current_stage,
            "status": status,
            "created_at": datetime.now(timezone.utc),
        }
        self._runs_by_project.setdefault(project_id, []).append(row)
        return dict(row)

