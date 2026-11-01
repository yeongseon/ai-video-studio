from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, assert_never

from creator_domain.models import EncodingProfile, MediaAsset, OutputSpec, RenderPlan, RenderSegmentKind
from creator_service.ffmpeg_service import FFmpegService, RenderInput, _run_ffmpeg
from creator_service.render_profile import RenderProfile
from creator_service.render_segments import render_video_segment
from creator_service.timeline_compiler import AssetResolver, compile_timeline_to_render_plan
from pydantic import BaseModel, ConfigDict, Field, Json, ValidationError
from tasks.render_materializer import RenderSourceError, materialize_plan
from tasks.task_runner import TaskContext


class RenderMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    render_source: str | None = None
    render_timeline_revision: int | None = Field(default=None, ge=1)


class TimelineApprovalNotes(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    timeline_id: str = Field(min_length=1)
    revision: int = Field(ge=1)


class TimelineApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    run_id: int = Field(ge=1)
    stage_name: Literal["TIMELINE_REVIEW"]
    review_status: Literal["approved"]
    notes: Json[TimelineApprovalNotes]


@dataclass(frozen=True, slots=True)
class RenderLayers:
    audio: Path | None = None
    subtitles: Path | None = None


def read_render_metadata(ctx: TaskContext) -> RenderMetadata:
    raw = ctx.run.get("metadata_json") or ctx.run.get("metadata") or {}
    try:
        if isinstance(raw, str):
            return RenderMetadata.model_validate_json(raw)
        return RenderMetadata.model_validate(raw)
    except ValidationError as exc:
        raise RenderSourceError("Invalid render approval metadata") from exc


class ResolvedAssets:
    def __init__(self, resolver: AssetResolver) -> None:
        self.resolver = resolver
        self.assets: list[MediaAsset] = []

    async def get_asset(self, asset_id: int, workspace_id: int) -> MediaAsset | None:
        asset = await self.resolver.get_asset(asset_id, workspace_id)
        if asset is not None:
            self.assets.append(asset)
        return asset


class TimelineFFmpegService(FFmpegService):
    """Reuse legacy concat/audio/subtitles, replacing only video-segment encoding."""

    def __init__(self, plan: RenderPlan, profile: RenderProfile) -> None:
        super().__init__(profile)
        self._segments = iter(plan.segments)

    def _render_segment(
        self, image_path: Path, duration: float, output_path: Path,
        transition_override: str | None = None,
    ) -> None:
        segment = next(self._segments)
        match segment.kind:
            case RenderSegmentKind.IMAGE:
                super()._render_segment(image_path, duration, output_path, transition_override)
            case RenderSegmentKind.VIDEO:
                render_video_segment(segment, output_path, profile=self.profile,
                                     frame_budget=round(duration * self.profile.fps))
            case unreachable:
                assert_never(unreachable)


async def render_saved_timeline(
    ctx: TaskContext, profile: RenderProfile, output: Path, *, layers: RenderLayers = RenderLayers(),
) -> int:
    """Production timeline worker path; returns only after actual FFmpeg completion."""
    from creator_service.media_asset_service import media_asset_service
    from creator_service.object_storage import get_storage_backend
    from creator_service.stage_review_service import stage_review_service
    from creator_service.timeline_service import timeline_service

    if ctx.workspace_id is None or ctx.project_id is None:
        raise RenderSourceError("Timeline render requires project/workspace context")
    approved = read_render_metadata(ctx).render_timeline_revision
    if approved is None:
        raise RenderSourceError("Timeline render requires an approved revision")
    timeline = await timeline_service.load_timeline(
        project_id=ctx.project_id, workspace_id=ctx.workspace_id,
    )
    if timeline is None or timeline.project_id != ctx.project_id:
        raise RenderSourceError("Approved timeline is unavailable")
    if timeline.revision != approved:
        raise RenderSourceError("Timeline revision changed after approval")
    review = await stage_review_service.get_latest_review(ctx.run_id, "TIMELINE_REVIEW")
    try:
        approval = TimelineApprovalRecord.model_validate(review)
    except ValidationError as exc:
        raise RenderSourceError("Timeline render requires a valid persisted approval") from exc
    if (
        approval.run_id != ctx.run_id
        or approval.notes.timeline_id != timeline.id
        or approval.notes.revision != approved
    ):
        raise RenderSourceError("Timeline approval does not match the run, timeline or revision")
    resolver = ResolvedAssets(media_asset_service)
    plan = await compile_timeline_to_render_plan(
        timeline, workspace_id=ctx.workspace_id, asset_resolver=resolver,
        output_spec=OutputSpec.from_render_profile(profile),
        encoding_profile=EncodingProfile.from_render_profile(profile),
    )
    cursor = 0.0
    for segment in plan.segments:
        if abs(segment.timeline_start_seconds - cursor) > 1e-6:
            raise RenderSourceError("Timeline gaps are not supported by this renderer")
        cursor += segment.duration_seconds
        if segment.trim_end_seconds is not None:
            source_duration = segment.trim_end_seconds - (segment.trim_start_seconds or 0)
            if abs(source_duration - segment.duration_seconds) > 1e-6:
                raise RenderSourceError("Timeline trim duration must match placement duration")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="timeline-", dir=output.parent) as directory:
        local_plan = materialize_plan(
            plan, resolver.assets, Path(directory), backend=get_storage_backend(),
            storage_provider=os.getenv("STORAGE_BACKEND", "local"),
        )
        renderer = TimelineFFmpegService(local_plan, profile)
        audio = layers.audio
        if audio is not None:
            audio = Path(directory) / "narration.wav"
            result = _run_ffmpeg([
                "ffmpeg", "-y", "-nostdin", "-i", str(layers.audio),
                "-af", "apad", "-t", str(plan.total_duration_seconds), str(audio),
            ], timeout=180)
            if result.returncode != 0:
                raise RenderSourceError("Timeline narration preparation failed")
        renderer.render(RenderInput(
            image_paths=[Path(segment.source) for segment in local_plan.segments],
            scene_durations=[segment.duration_seconds for segment in local_plan.segments],
            scene_transitions=[segment.transition or "cut" for segment in local_plan.segments],
            audio_path=audio, subtitle_path=layers.subtitles,
        ), output)
    return len(plan.segments)
