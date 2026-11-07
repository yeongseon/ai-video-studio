from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeAlias

import pytest
from creator_domain.models import MediaAsset, MediaOrigin, MediaSegment, MediaType, Timeline
from creator_service import artifact_storage_integration
from creator_service.audio_service import AudioService
from creator_service.media_asset_service import InMemoryMediaAssetStorage, MediaAssetService
from creator_service.object_storage import LocalStorageBackend
from creator_service.render_profile import RenderProfile
from creator_service.render_service import InMemoryRenderStorage, RenderService
from creator_service.script_service import ScriptService
from creator_service.stage_review_service import (
    InMemoryStageReviewStorage,
    StageReviewService,
)
from creator_service.subtitle_service import SubtitleService
from creator_service.timeline_service import TimelineService
from creator_service.visual_asset_service import VisualAssetService
from creator_service.visual_plan_service import VisualPlanService
from tasks import render_video
from tasks.task_runner import TaskContext

StorageMetadata: TypeAlias = dict[str, str]


@dataclass(frozen=True, slots=True)
class RenderedAdapterPaths:
    legacy: Path
    timeline: Path
    profile: RenderProfile
    legacy_metadata: StorageMetadata
    timeline_metadata: StorageMetadata
    timeline_context: TaskContext
    timeline_service: TimelineService
    review_service: StageReviewService


@dataclass(frozen=True, slots=True)
class _AssetOwner:
    assets: MediaAssetService

    async def get_asset_owner(self, asset_id: int, workspace_id: int) -> int | None:
        asset = await self.assets.get_asset(asset_id, workspace_id)
        return asset.project_id if asset is not None else None


async def render_through_production_adapters(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    image: Path,
    audio: Path | None,
    subtitles: Path,
) -> RenderedAdapterPaths:
    backend = LocalStorageBackend(str(root))
    render_storage = InMemoryRenderStorage()
    renders = RenderService(render_storage)
    audio_service = AudioService()
    subtitle_service = SubtitleService()
    visuals = VisualAssetService()
    assets = MediaAssetService(backend, InMemoryMediaAssetStorage())
    timelines = TimelineService(asset_owner=_AssetOwner(assets))
    reviews = StageReviewService(InMemoryStageReviewStorage())
    profile = RenderProfile.default()

    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setattr(render_video, "_ARTIFACT_ROOT", str(root))
    monkeypatch.setattr(render_video, "_render_service", renders)
    monkeypatch.setattr(render_video, "_audio_service", audio_service)
    monkeypatch.setattr(render_video, "_subtitle_service", subtitle_service)
    monkeypatch.setattr(render_video, "_visual_asset_service", visuals)
    monkeypatch.setattr(render_video, "_visual_plan_service", VisualPlanService())
    monkeypatch.setattr(render_video, "_script_service", ScriptService())
    monkeypatch.setattr(artifact_storage_integration, "_ARTIFACT_ROOT", root.resolve())
    monkeypatch.setattr("creator_service.object_storage._storage_backend", backend)
    monkeypatch.setattr("creator_service.media_asset_service.media_asset_service", assets)
    monkeypatch.setattr("creator_service.timeline_service.timeline_service", timelines)
    monkeypatch.setattr("creator_service.stage_review_service.stage_review_service", reviews)

    async def authorize(*_args: str | None) -> bool:
        return True

    async def record(*_args: str | int | float | None, **_kwargs: str | int | float | None) -> None:
        return None

    monkeypatch.setattr(render_video, "authorize_owned_provider_attempt", authorize)
    monkeypatch.setattr(render_video, "record_provider_call", record)

    legacy_run_id = 1
    await visuals.create_asset(legacy_run_id, "scene-1", str(image))
    if audio is not None:
        await audio_service.create_artifact(legacy_run_id, str(audio))
    await subtitle_service.create_artifact(legacy_run_id, str(subtitles))
    legacy_context = TaskContext(
        run_id=legacy_run_id,
        task_id="legacy-parity",
        run={"metadata": {}},
        workspace_id=1,
        project_id=1,
        start_time=datetime.now(timezone.utc),
    )
    legacy_result = await render_video.execute_render(legacy_context)
    legacy = Path(str(legacy_result.extra["video_path"]))
    legacy_row = await render_storage.get_latest_by_run(legacy_run_id)
    assert legacy_row is not None

    timeline_run_id = 2
    source_key = "workspaces/1/assets/parity-scene.png"
    backend.upload(source_key, image.read_bytes(), content_type="image/png")
    saved = await assets._asset_storage.save_asset(
        MediaAsset(
            id=1,
            workspace_id=1,
            project_id=1,
            media_type=MediaType.IMAGE,
            origin=MediaOrigin.UPLOADED,
            storage_key=source_key,
            metadata={"storage_provider": "local"},
            created_at=datetime.now(timezone.utc),
        ).model_dump(exclude={"id"})
    )
    asset = MediaAsset.model_validate(saved)
    timeline = await timelines.save_timeline(
        project_id=1,
        workspace_id=1,
        expected_revision=0,
        timeline=Timeline(
            id="saved-timeline",
            project_id=1,
            segments=[
                MediaSegment(
                    id="segment-1",
                    scene_id="scene-1",
                    asset_id=asset.id,
                    timeline_start_seconds=0,
                    duration_seconds=1,
                )
            ],
        ),
    )
    await reviews.record_approval(
        timeline_run_id,
        "TIMELINE_REVIEW",
        notes=json.dumps({"timeline_id": timeline.id, "revision": timeline.revision}),
    )
    if audio is not None:
        await audio_service.create_artifact(timeline_run_id, str(audio))
    await subtitle_service.create_artifact(timeline_run_id, str(subtitles))
    timeline_context = TaskContext(
        run_id=timeline_run_id,
        task_id="timeline-parity",
        run={
            "metadata": {
                "render_source": "timeline",
                "render_timeline_revision": timeline.revision,
            }
        },
        workspace_id=1,
        project_id=1,
        start_time=datetime.now(timezone.utc),
    )
    timeline_result = await render_video.execute_render(timeline_context)
    timeline_output = Path(str(timeline_result.extra["video_path"]))
    timeline_row = await render_storage.get_latest_by_run(timeline_run_id)
    assert timeline_row is not None

    return RenderedAdapterPaths(
        legacy=legacy,
        timeline=timeline_output,
        profile=profile,
        legacy_metadata={
            key: str(value) for key, value in legacy_row["metadata_json"].items()
        },
        timeline_metadata={
            key: str(value) for key, value in timeline_row["metadata_json"].items()
        },
        timeline_context=timeline_context,
        timeline_service=timelines,
        review_service=reviews,
    )
