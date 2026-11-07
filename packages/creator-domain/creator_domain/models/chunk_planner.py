from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from creator_domain.exceptions import ChunkManifestValidationError

from .chunk_manifest import (
    SCHEMA_VERSION,
    ChunkManifest,
    ChunkSourceRef,
    RenderChunk,
)
from .chunk_identity import ChunkPlanningInput, SourceAssetIdentity
from .chunk_policy import ChunkEncodingProfile, ChunkOutputSpec, ChunkPolicy
from .frame_budget import cumulative_frame_ends, require_contiguous_placements
from .render_plan import RenderPlan


@dataclass(frozen=True, slots=True)
class _SegmentFrames:
    index: int
    source: str
    source_asset: SourceAssetIdentity
    start: int
    end: int


def plan_chunks(
    planning_input: ChunkPlanningInput | RenderPlan,
    fps: int | Fraction,
    policy: ChunkPolicy | None = None,
) -> ChunkManifest:
    """Plan deterministic chunks on the same cumulative CFR budget as rendering."""
    match planning_input:
        case ChunkPlanningInput():
            identified_input = planning_input
        case RenderPlan():
            raise ChunkManifestValidationError("immutable planning identity is required")
    identified_input.validate()
    render_plan = identified_input.render_plan
    frame_rate = _integer_fps(fps)
    if render_plan.output_spec.fps != frame_rate:
        raise ChunkManifestValidationError("fps must match the render plan output specification")
    resolved = (policy or ChunkPolicy()).resolve(frame_rate)
    segments = _allocate_segment_frames(identified_input, frame_rate)
    chunks = _partition(segments, frame_rate, resolved.max_chunk_frames, resolved.audio_sample_rate)
    if len(chunks) > resolved.max_chunk_count:
        raise ChunkManifestValidationError("chunk count exceeds the configured policy")
    return ChunkManifest(
        schema_version=SCHEMA_VERSION,
        approval=identified_input.approval,
        total_frames=segments[-1].end,
        fps=frame_rate,
        output_spec=ChunkOutputSpec.from_output_spec(render_plan.output_spec),
        encoding_profile=ChunkEncodingProfile.from_encoding_profile(render_plan.encoding_profile),
        policy=resolved,
        chunks=chunks,
    )


def _integer_fps(value: int | Fraction) -> int:
    frame_rate = Fraction(value)
    if frame_rate.denominator != 1 or frame_rate < 1:
        raise ChunkManifestValidationError("fps must be a positive integer or integer Fraction")
    return frame_rate.numerator


def _allocate_segment_frames(planning_input: ChunkPlanningInput, fps: int) -> tuple[_SegmentFrames, ...]:
    identified = zip(
        planning_input.render_plan.segments,
        planning_input.source_assets,
        strict=True,
    )
    ordered = sorted(identified, key=lambda item: item[0].timeline_start_seconds)
    durations = [segment.duration_seconds for segment, _ in ordered]
    require_contiguous_placements(
        [(segment.timeline_start_seconds, segment.duration_seconds) for segment, _ in ordered],
        fps,
    )
    ends = cumulative_frame_ends(durations, fps)
    starts = [0, *ends[:-1]]
    allocated = tuple(
        _SegmentFrames(
            index=index,
            source=segment.source,
            source_asset=source_asset,
            start=start,
            end=end,
        )
        for index, ((segment, source_asset), start, end) in enumerate(
            zip(ordered, starts, ends, strict=True)
        )
    )
    if any(segment.end <= segment.start for segment in allocated):
        raise ChunkManifestValidationError(
            "each render segment must occupy at least one output frame"
        )
    return allocated


def _partition(
    segments: tuple[_SegmentFrames, ...],
    fps: int,
    max_frames: int,
    sample_rate: int,
) -> tuple[RenderChunk, ...]:
    boundaries = [0]
    for segment in segments:
        if segment.end - boundaries[-1] <= max_frames:
            continue
        if segment.start > boundaries[-1]:
            boundaries.append(segment.start)
        while segment.end - boundaries[-1] > max_frames:
            boundaries.append(boundaries[-1] + max_frames)
    boundaries.append(segments[-1].end)
    unique_boundaries = tuple(dict.fromkeys(boundaries))
    return tuple(
        _chunk(index, start, end, segments, fps, sample_rate)
        for index, (start, end) in enumerate(
            zip(unique_boundaries, unique_boundaries[1:], strict=False)
        )
    )


def _chunk(
    index: int,
    start: int,
    end: int,
    segments: tuple[_SegmentFrames, ...],
    fps: int,
    sample_rate: int,
) -> RenderChunk:
    refs = tuple(
        ChunkSourceRef(
            segment_index=segment.index,
            source=segment.source,
            asset_id=segment.source_asset.asset_id,
            content_revision=segment.source_asset.content_revision,
            start_frame=max(start, segment.start),
            end_frame=min(end, segment.end),
        )
        for segment in segments
        if segment.start < end and segment.end > start
    )
    return RenderChunk(
        index=index,
        start_frame=start,
        end_frame=end,
        start_s=Fraction(start, fps),
        end_s=Fraction(end, fps),
        start_sample=round(Fraction(start * sample_rate, fps)),
        end_sample=round(Fraction(end * sample_rate, fps)),
        source_refs=refs,
    )
