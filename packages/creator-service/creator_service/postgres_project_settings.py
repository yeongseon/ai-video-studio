from datetime import datetime
import json
from typing import Any

from creator_domain.models.project_settings import ProjectSettings

from .db import fetch_one, transaction


async def fetch_settings(project_id: int, workspace_id: int | None) -> ProjectSettings | None:
    row = await fetch_one("""
        SELECT s.settings FROM creator_project_settings s
        JOIN creator_projects p ON p.id = s.project_id
        WHERE p.id = $1 AND ($2::bigint IS NULL OR (p.workspace_id = $2 AND s.workspace_id = $2))
    """, project_id, workspace_id)
    return ProjectSettings.model_validate_json(row["settings"]) if row is not None else None


async def insert_with_settings(payload: dict[str, Any]) -> dict[str, Any]:
    async with transaction() as connection:
        row = await connection.fetchrow("""
            INSERT INTO creator_projects (title, source_type, idea_brief, markdown_source,
                url_source, json_script, status, workspace_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING *
        """, payload.get("title"), payload.get("source_type", "idea"), payload.get("idea_brief"),
            payload.get("markdown_source"), payload.get("url_source"), payload.get("json_script"),
            payload.get("status", "draft"), payload.get("workspace_id"))
        if row is None:
            raise RuntimeError("Project insert returned no row")
        await connection.execute("""
            INSERT INTO creator_project_settings (project_id, workspace_id, settings)
            VALUES ($1,$2,$3::jsonb)
        """, row["id"], row["workspace_id"], json.dumps(payload["settings"]))
        return dict(row)


async def replace_settings(project_id: int, settings: ProjectSettings, *,
                           workspace_id: int | None, expected_updated_at: datetime | None) -> dict[str, Any] | None:
    async with transaction() as connection:
        row = await connection.fetchrow("""
            UPDATE creator_projects SET updated_at = NOW()
            WHERE id = $1 AND workspace_id = $2 AND updated_at = $3 RETURNING *
        """, project_id, workspace_id, expected_updated_at)
        if row is None:
            return None
        await connection.execute("""
            INSERT INTO creator_project_settings (project_id, workspace_id, settings)
            VALUES ($1,$2,$3::jsonb)
            ON CONFLICT (project_id) DO UPDATE SET settings=EXCLUDED.settings,
                workspace_id=EXCLUDED.workspace_id, updated_at=NOW()
        """, project_id, workspace_id, settings.model_dump_json())
        return dict(row)
