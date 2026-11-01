from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from .db import fetch_all, fetch_one


class PostgresProjectStorage:
    async def insert_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = await fetch_one(
            """
            INSERT INTO creator_projects (
                title,
                source_type,
                idea_brief,
                markdown_source,
                url_source,
                json_script,
                status,
                workspace_id,
                settings
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            RETURNING *
            """,
            payload.get("title"),
            payload.get("source_type", "idea"),
            payload.get("idea_brief"),
            payload.get("markdown_source"),
            payload.get("url_source"),
            payload.get("json_script"),
            payload.get("status", "draft"),
            payload.get("workspace_id"),
            json.dumps(payload["settings"]) if payload.get("settings") is not None else None,
        )
        if row is None:
            raise ValueError("Failed to insert project")
        return _decode_project(row)

    async def fetch_project(
        self, project_id: int, workspace_id: int | None = None
    ) -> dict[str, Any] | None:
        if workspace_id is None:
            row = await fetch_one("SELECT * FROM creator_projects WHERE id = $1", project_id)
        else:
            row = await fetch_one(
                "SELECT * FROM creator_projects WHERE id = $1 AND workspace_id = $2",
                project_id, workspace_id,
            )
        return _decode_project(row) if row is not None else None

    async def list_projects(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]:
        if workspace_id is None:
            rows = await fetch_all(
                """
                SELECT *
                FROM creator_projects
                ORDER BY created_at DESC, id DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        else:
            rows = await fetch_all(
                """
                SELECT *
                FROM creator_projects
                WHERE workspace_id = $1
                ORDER BY created_at DESC, id DESC
                LIMIT $2 OFFSET $3
                """,
                workspace_id,
                limit,
                offset,
            )
        return [_decode_project(row) for row in rows]

    async def list_projects_with_latest_run(
        self, limit: int, offset: int, workspace_id: int | None = None
    ) -> list[dict[str, Any]]:
        if workspace_id is None:
            rows = await fetch_all(
                """
                SELECT
                    p.*, lr.run_id,
                    lr.current_stage AS latest_run_current_stage,
                    lr.status AS latest_run_status
                FROM creator_projects p
                LEFT JOIN LATERAL (
                    SELECT id AS run_id, current_stage, status
                    FROM creator_runs
                    WHERE project_id = p.id
                    ORDER BY id DESC
                    LIMIT 1
                ) lr ON true
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT $1 OFFSET $2
                """,
                limit,
                offset,
            )
        else:
            rows = await fetch_all(
                """
                SELECT
                    p.*, lr.run_id,
                    lr.current_stage AS latest_run_current_stage,
                    lr.status AS latest_run_status
                FROM creator_projects p
                LEFT JOIN LATERAL (
                    SELECT id AS run_id, current_stage, status
                    FROM creator_runs
                    WHERE project_id = p.id
                    ORDER BY id DESC
                    LIMIT 1
                ) lr ON true
                WHERE p.workspace_id = $1
                ORDER BY p.created_at DESC, p.id DESC
                LIMIT $2 OFFSET $3
                """,
                workspace_id,
                limit,
                offset,
            )

        _LATERAL_KEYS = {"run_id", "latest_run_current_stage", "latest_run_status"}
        projects: list[dict[str, Any]] = []
        for row in rows:
            latest_run = None
            if row["run_id"] is not None:
                latest_run = {
                    "run_id": row["run_id"],
                    "current_stage": row["latest_run_current_stage"],
                    "status": row["latest_run_status"],
                }
            project_data = {k: v for k, v in dict(row).items() if k not in _LATERAL_KEYS}
            project_data["latest_run"] = latest_run
            projects.append(_decode_project(project_data))
        return projects

    async def count_projects(self, workspace_id: int | None = None) -> int:
        if workspace_id is None:
            row = await fetch_one("SELECT COUNT(*) AS count FROM creator_projects")
        else:
            row = await fetch_one(
                "SELECT COUNT(*) AS count FROM creator_projects WHERE workspace_id = $1",
                workspace_id,
            )
        if row is None:
            return 0
        return int(row["count"])

    async def fetch_latest_run_summary(self, project_id: int) -> dict[str, int | str | None] | None:
        row = await fetch_one(
            """
            SELECT id AS run_id, current_stage, status
            FROM creator_runs
            WHERE project_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            project_id,
        )
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "current_stage": row["current_stage"],
            "status": row["status"],
        }

    _ALLOWED_COLUMNS = frozenset(
        {
            "title",
            "source_type",
            "idea_brief",
            "markdown_source",
            "url_source",
            "json_script",
            "status",
            "workspace_id",
            "settings",
        }
    )

    async def update_project(
        self,
        project_id: int,
        updates: dict[str, Any],
        *,
        workspace_id: int | None = None,
        expected_updated_at: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Update project fields. Returns updated row or None if not found.

        Only columns listed in ``_ALLOWED_COLUMNS`` are accepted; unknown
        keys are silently dropped to prevent SQL injection via dynamic column names.
        """
        safe_updates = {k: v for k, v in updates.items() if k in self._ALLOWED_COLUMNS}
        if not safe_updates:
            return await self.fetch_project(project_id, workspace_id=workspace_id)
        set_clauses = []
        params: list[Any] = []
        idx = 1
        for key, value in safe_updates.items():
            set_clauses.append(f"{key} = ${idx}")
            params.append(json.dumps(value) if key == "settings" and value is not None else value)
            idx += 1
        params.append(project_id)
        where_clause = f"id = ${idx}"
        if workspace_id is not None:
            idx += 1
            params.append(workspace_id)
            where_clause += f" AND workspace_id = ${idx}"
        if expected_updated_at is not None:
            idx += 1
            params.append(expected_updated_at)
            where_clause += f" AND updated_at = ${idx}"
        query = f"""
            UPDATE creator_projects
            SET {", ".join(set_clauses)}, updated_at = NOW()
            WHERE {where_clause}
            RETURNING *
        """
        row = await fetch_one(query, *params)
        return _decode_project(row) if row is not None else None

    async def delete_project(self, project_id: int, *, workspace_id: int | None = None) -> bool:
        """Delete a project by id. Returns True if deleted.

        Runs are cascade-deleted by FK constraint.
        """
        if workspace_id is None:
            result = await fetch_one(
                "DELETE FROM creator_projects WHERE id = $1 RETURNING id",
                project_id,
            )
        else:
            result = await fetch_one(
                "DELETE FROM creator_projects WHERE id = $1 AND workspace_id = $2 RETURNING id",
                project_id,
                workspace_id,
            )
        return result is not None


def _decode_project(row: dict[str, Any]) -> dict[str, Any]:
    from creator_domain.models.project_settings import ProjectSettings

    raw = row.get("settings")
    if raw is not None:
        settings = ProjectSettings.model_validate_json(raw) if isinstance(raw, str) else ProjectSettings.model_validate(raw)
        return {**row, "settings": settings.model_dump(mode="json")}
    return row
