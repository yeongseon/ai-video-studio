"""Real API Timeline fixtures; uploads have no project-assignment endpoint yet."""

from __future__ import annotations

from pathlib import Path
import subprocess

import anyio
import asyncpg
import httpx

from studio_proxy_runtime_support import StudioProxyRuntime


async def _assign_project(runtime: StudioProxyRuntime, asset_id: int, project_id: int) -> None:
    connection = await asyncpg.connect(runtime.database_url, server_settings={"search_path": runtime.schema})
    try:
        await connection.execute(
            "UPDATE creator_media_assets SET project_id=$1 WHERE id=$2 AND workspace_id=$3",
            project_id, asset_id, runtime.workspace_id,
        )
    finally:
        await connection.close()


def seed_mixed_timeline(runtime: StudioProxyRuntime, root: Path) -> tuple[int, Path]:
    video = root / "colors.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=red:s=96x96:r=30:d=3,drawbox=color=green:t=fill:enable='between(t,1,2)',"
        "drawbox=color=blue:t=fill:enable='gte(t,2)'",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1", "-g", "30", str(video),
    ], check=True, capture_output=True, timeout=10)
    with httpx.Client(base_url=runtime.origin, headers={"Origin": runtime.origin}, timeout=10) as client:
        seed = client.post(f"/api/creator/workspaces/{runtime.workspace_id}/demo-short/runs")
        assert seed.status_code == 201, seed.text
        project_id = int(seed.json()["seeded_project_id"])
        endpoint = f"/api/creator/projects/{project_id}/timeline"
        existing = client.get(endpoint)
        assert existing.status_code == 200, existing.text
        timeline = existing.json()
        image_id = timeline["segments"][0]["asset_id"]
        with video.open("rb") as stream:
            uploaded = client.post(
                f"/api/creator/workspaces/{runtime.workspace_id}/assets/videos",
                files={"file": ("colors.mp4", stream, "video/mp4")},
            )
        assert uploaded.status_code == 201, uploaded.text
        asset = uploaded.json()
        anyio.run(_assign_project, runtime, int(asset["id"]), project_id)
        with video.open("rb") as stream:
            later = client.post(f"/api/creator/workspaces/{runtime.workspace_id}/assets/videos",
                                files={"file": ("later.mp4", stream, "video/mp4")})
        assert later.status_code == 201, later.text
        later_asset = later.json()
        anyio.run(_assign_project, runtime, int(later_asset["id"]), project_id)
        segments = [
            {"id": "before", "scene_id": "scene", "asset_id": image_id,
             "timeline_start_seconds": 0, "duration_seconds": 0.5},
            {"id": "trimmed", "scene_id": "scene", "asset_id": asset["id"],
             "timeline_start_seconds": 0.5, "duration_seconds": 1.5,
             "trim_start_seconds": 1, "trim_end_seconds": 2.5, "fit_mode": "contain"},
            {"id": "after", "scene_id": "scene", "asset_id": image_id,
             "timeline_start_seconds": 2, "duration_seconds": 0.5},
            {"id": "same-source", "scene_id": "scene", "asset_id": asset["id"],
             "timeline_start_seconds": 2.5, "duration_seconds": 0.5,
             "trim_start_seconds": 0.2, "trim_end_seconds": 0.7},
            {"id": "consecutive-same-source", "scene_id": "scene", "asset_id": asset["id"],
             "timeline_start_seconds": 3, "duration_seconds": 0.5,
             "trim_start_seconds": 2, "trim_end_seconds": 2.5},
            {"id": "decode-failure", "scene_id": "scene", "asset_id": later_asset["id"],
             "timeline_start_seconds": 3.5, "duration_seconds": 0.5},
        ]
        saved = client.put(endpoint, json={"expected_revision": timeline["revision"], "timeline": {
            "id": timeline["id"], "project_id": project_id, "segments": segments,
        }})
        assert saved.status_code == 200, saved.text
        return project_id, runtime.artifact_root / later_asset["storage_key"]
