from __future__ import annotations

import random
from fractions import Fraction

import pytest

from creator_domain.exceptions import ChunkManifestValidationError
from creator_domain.models import (
    ChunkPolicy,
    ApprovalIdentity,
    ChunkPlanningInput,
    EncodingProfile,
    OutputSpec,
    RenderPlan,
    RenderSegment,
    RenderSegmentKind,
    SourceAssetIdentity,
    plan_chunks,
)


def _plan(placements: list[tuple[Fraction, Fraction]], fps: int) -> RenderPlan:
    return RenderPlan(
        segments=[
            RenderSegment(
                kind=RenderSegmentKind.IMAGE,
                source=f"assets/scene-{index}.png",
                timeline_start_seconds=float(start),
                duration_seconds=float(duration),
            )
            for index, (start, duration) in enumerate(placements)
        ],
        output_spec=OutputSpec(width=1920, height=1080, fps=fps),
        encoding_profile=EncodingProfile.standard(),
    )


def _input(placements: list[tuple[Fraction, Fraction]], fps: int) -> ChunkPlanningInput:
    return ChunkPlanningInput(
        render_plan=_plan(placements, fps),
        approval=ApprovalIdentity(timeline_id="timeline-regression", revision=1),
        source_assets=tuple(
            SourceAssetIdentity(asset_id=index + 1, content_revision=1)
            for index in range(len(placements))
        ),
    )


def _unchecked_input(
    placements: list[tuple[Fraction, Fraction]],
    fps: int,
) -> ChunkPlanningInput:
    render_plan = RenderPlan.model_construct(
        segments=[
            RenderSegment(
                kind=RenderSegmentKind.IMAGE,
                source=f"assets/scene-{index}.png",
                timeline_start_seconds=float(start),
                duration_seconds=float(duration),
            )
            for index, (start, duration) in enumerate(placements)
        ],
        output_spec=OutputSpec(width=1920, height=1080, fps=fps),
        encoding_profile=EncodingProfile.standard(),
    )
    return ChunkPlanningInput(
        render_plan=render_plan,
        approval=ApprovalIdentity(timeline_id="timeline-regression", revision=1),
        source_assets=tuple(
            SourceAssetIdentity(asset_id=index + 1, content_revision=1)
            for index in range(len(placements))
        ),
    )


@pytest.mark.parametrize(
    "second_start",
    [Fraction(2), Fraction(1, 2)],
    ids=["gap", "overlap"],
)
def test_plan_chunks_rejects_non_contiguous_placements(second_start: Fraction) -> None:
    # Given two segments whose explicit placements are not exactly contiguous.
    planning_input = _unchecked_input(
        [(Fraction(0), Fraction(1)), (second_start, Fraction(1))],
        fps=30,
    )

    # When planning the frame grid, then the discarded gap or overlap is rejected.
    with pytest.raises(ChunkManifestValidationError, match="contiguous"):
        _ = plan_chunks(planning_input, 30)


def test_plan_chunks_rounds_exact_half_frame_total_to_even() -> None:
    # Given source durations whose exact rational total is 15.5 frames at 24 fps.
    planning_input = _input(
        [(Fraction(0), Fraction(1, 3)), (Fraction(1, 3), Fraction(5, 16))],
        fps=24,
    )

    # When planned, then exact ties-to-even rounding produces 16 frames.
    manifest = plan_chunks(planning_input, 24)

    assert manifest.total_frames == 16


@pytest.mark.parametrize("fps", [24, 25, 30, 60])
@pytest.mark.parametrize("whole_frames", [10, 11, 100, 101])
@pytest.mark.parametrize("offset", [Fraction(-1, 1_000_000), Fraction(), Fraction(1, 1_000_000)])
def test_plan_chunks_rounds_boundaries_around_half_frames(
    fps: int,
    whole_frames: int,
    offset: Fraction,
) -> None:
    # Given cumulative boundaries immediately below, at, and above a half frame.
    duration = Fraction(whole_frames * 2 + 1, fps * 2) + offset

    # When planned, then every cumulative boundary follows exact ties-to-even rounding.
    manifest = plan_chunks(_input([(Fraction(), duration)], fps), fps)

    assert manifest.total_frames == round(duration * fps)


@pytest.mark.parametrize("fps", [24, 25, 30, 60])
@pytest.mark.parametrize("minutes", [10, 15, 20])
def test_plan_chunks_preserves_exact_long_form_budget(fps: int, minutes: int) -> None:
    # Given deterministic 10-20 minute inputs containing fractional-frame boundaries.
    rng = random.Random(fps * 10_000 + minutes)
    total = Fraction(minutes * 60)
    durations: list[Fraction] = []
    remaining = total
    while remaining:
        duration = min(remaining, Fraction(rng.randint(fps * 3, fps * 17) * 2 + 1, fps * 2))
        durations.append(duration)
        remaining -= duration
    start = Fraction()
    placements: list[tuple[Fraction, Fraction]] = []
    for duration in durations:
        placements.append((start, duration))
        start += Fraction.from_float(float(duration)).limit_denominator(1_000_000_000)

    # When planned, then the final frame equals the exact cumulative budget.
    manifest = plan_chunks(_input(placements, fps), fps, ChunkPolicy(max_chunk_seconds=90))

    assert manifest.total_frames == round(total * fps)
