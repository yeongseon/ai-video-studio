from __future__ import annotations

from pathlib import Path

from restore_drill_runtime import RuntimeFixture


def timeline_render_program(restored: Path, fixture: RuntimeFixture) -> str:
    return f"""
import anyio, json
from datetime import datetime, timezone
from pathlib import Path
from creator_service.render_profile import RenderProfile
from creator_service.audio_service import audio_service
from creator_service.render_service import render_service
from tasks.task_runner import TaskContext
from tasks.timeline_render import RenderLayers, render_saved_timeline

async def main():
    artifact_root = Path({str(restored)!r})
    output = artifact_root / 'final.mp4'
    context = TaskContext(
        run_id={fixture.run_id}, task_id='restore-drill-render',
        run={{'metadata_json': {{'render_source': 'timeline', 'render_timeline_revision': 1}}}},
        workspace_id={fixture.workspace_id}, project_id={fixture.project_id},
        start_time=datetime.now(timezone.utc),
    )
    narration = await audio_service.get_latest(context.run_id)
    if narration is None:
        raise RuntimeError('restored audio metadata did not resolve narration')
    audio_path = artifact_root / narration.path
    if not audio_path.is_file():
        raise RuntimeError('restored audio metadata did not resolve narration')
    count = await render_saved_timeline(
        context, RenderProfile.fast_preview(), output,
        layers=RenderLayers(audio=audio_path),
    )
    artifact = await render_service.create_artifact(
        run_id={fixture.run_id}, path=str(output), render_profile='restore_drill',
        storage_provider='local', storage_key='final.mp4',
        idempotency_key='restore-drill-render',
    )
    print(json.dumps({{'count': count, 'artifact_id': artifact.id, 'audio_path': str(audio_path)}}))

anyio.run(main)
"""
