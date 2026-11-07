from __future__ import annotations

import json
import random
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
from typing import Callable

import pytest

from creator_domain.exceptions import ChunkManifestValidationError
from creator_domain.models import (
    ChunkManifest,
    ChunkPolicy,
    EncodingProfile,
    OutputSpec,
    RenderPlan,
    RenderSegment,
    RenderSegmentKind,
    plan_chunks,
)


def _plan(durations: list[Fraction], fps: int = 30) -> RenderPlan:
    start = Fraction()
    segments: list[RenderSegment] = []
    for index, duration in enumerate(durations):
        segments.append(
            RenderSegment(
                kind=RenderSegmentKind.IMAGE,
                source=f"assets/scene-{index}.png",
                timeline_start_seconds=float(start),
                duration_seconds=float(duration),
            )
        )
        start += duration
    return RenderPlan(
        segments=segments,
        output_spec=OutputSpec(width=1920, height=1080, fps=fps),
        encoding_profile=EncodingProfile.standard(),
    )


@pytest.mark.parametrize("fps", [24, 25, 30, 60])
@pytest.mark.parametrize("minutes", [10, 13, 20])
def test_plan_chunks_covers_long_form_frame_budget_exactly(fps: int, minutes: int) -> None:
    # Given non-integral segment durations spanning a long-form render.
    rng = random.Random(minutes * 100 + fps)
    durations: list[Fraction] = []
    remaining = Fraction(minutes * 60)
    while remaining > 0:
        duration = min(remaining, Fraction(rng.randint(71, 389), 10))
        durations.append(duration)
        remaining -= duration

    # When chunk boundaries are planned.
    manifest = plan_chunks(_plan(durations, fps), fps, ChunkPolicy(max_chunk_seconds=90))

    # Then cumulative boundaries cover the exact rounded render budget once.
    expected_frames = round(sum(durations) * fps)
    assert manifest.total_frames == expected_frames
    assert sum(chunk.frame_count for chunk in manifest.chunks) == expected_frames
    assert manifest.chunks[0].start_frame == 0
    assert manifest.chunks[-1].end_frame == expected_frames
    assert all(
        left.end_frame == right.start_frame
        for left, right in zip(manifest.chunks, manifest.chunks[1:], strict=False)
    )
    assert [chunk.index for chunk in manifest.chunks] == list(range(len(manifest.chunks)))
    assert all(chunk.frame_count <= manifest.policy.max_chunk_frames for chunk in manifest.chunks)


def test_plan_chunks_uses_exact_time_and_audio_sample_boundaries() -> None:
    # Given durations whose individual float frame rounding would drift.
    manifest = plan_chunks(
        _plan([Fraction(1, 10)] * 5, fps=25),
        Fraction(25, 1),
        ChunkPolicy(max_chunk_frames=5, audio_sample_rate=48_000),
    )

    # When represented on rational frame and sample timelines, then no float enters the contract.
    assert manifest.total_frames == 12
    assert [(chunk.start_s, chunk.end_s) for chunk in manifest.chunks] == [
        (Fraction(0), Fraction(1, 5)),
        (Fraction(1, 5), Fraction(2, 5)),
        (Fraction(2, 5), Fraction(12, 25)),
    ]
    assert [(chunk.start_sample, chunk.end_sample) for chunk in manifest.chunks] == [
        (0, 9_600),
        (9_600, 19_200),
        (19_200, 23_040),
    ]


def test_plan_chunks_keeps_short_segment_atomic_and_splits_oversized_segment() -> None:
    # Given one short atomic segment and one segment larger than the policy bound.
    manifest = plan_chunks(
        _plan([Fraction(2), Fraction(13)], fps=10),
        10,
        ChunkPolicy(max_chunk_frames=50),
    )

    # When planned, then the short segment stays whole and only the oversized segment is split.
    assert [(chunk.start_frame, chunk.end_frame) for chunk in manifest.chunks] == [
        (0, 20),
        (20, 70),
        (70, 120),
        (120, 150),
    ]
    assert [ref.segment_index for ref in manifest.chunks[0].source_refs] == [0]
    assert all(chunk.source_refs[0].segment_index == 1 for chunk in manifest.chunks[1:])


def test_manifest_hash_is_canonical_stable_and_tracks_output_contract() -> None:
    # Given the same approved render contract planned independently.
    plan = _plan([Fraction(7, 3), Fraction(11, 3)], fps=30)
    first = plan_chunks(plan, 30, ChunkPolicy(max_chunk_seconds=60))
    second = plan_chunks(plan, Fraction(60, 2), ChunkPolicy(max_chunk_frames=1_800))

    # When serialized with different presentation ordering, then identity remains content-derived.
    reordered = json.dumps(json.loads(first.to_json()), sort_keys=False, indent=2)
    assert first.manifest_id == second.manifest_id
    assert ChunkManifest.from_json(reordered).manifest_id == first.manifest_id
    assert first.output_spec.width == 1920
    assert first.encoding_profile.video_codec == "libx264"


def test_manifest_and_nested_values_are_immutable() -> None:
    # Given a planned manifest, when callers attempt mutation, then every contract layer is frozen.
    manifest = plan_chunks(_plan([Fraction(10)]), 30, ChunkPolicy())
    with pytest.raises(FrozenInstanceError):
        setattr(manifest, "total_frames", 1)
    with pytest.raises(FrozenInstanceError):
        setattr(manifest.chunks[0], "end_frame", 1)
    assert isinstance(manifest.chunks, tuple)


def _unsupported_schema(manifest: ChunkManifest) -> ChunkManifest:
    return replace(manifest, schema_version=2)


def _zero_total(manifest: ChunkManifest) -> ChunkManifest:
    return replace(manifest, total_frames=0)


def _negative_start(manifest: ChunkManifest) -> ChunkManifest:
    return replace(
        manifest, chunks=(replace(manifest.chunks[0], start_frame=-1), *manifest.chunks[1:])
    )


def _zero_length(manifest: ChunkManifest) -> ChunkManifest:
    return replace(
        manifest, chunks=(replace(manifest.chunks[0], end_frame=0), *manifest.chunks[1:])
    )


def _gap(manifest: ChunkManifest) -> ChunkManifest:
    return replace(
        manifest, chunks=(manifest.chunks[0], replace(manifest.chunks[1], start_frame=31))
    )


def _overlap(manifest: ChunkManifest) -> ChunkManifest:
    return replace(
        manifest, chunks=(manifest.chunks[0], replace(manifest.chunks[1], start_frame=29))
    )


def _duplicate_index(manifest: ChunkManifest) -> ChunkManifest:
    return replace(manifest, chunks=(manifest.chunks[0], replace(manifest.chunks[1], index=0)))


def _exceeds_bound(manifest: ChunkManifest) -> ChunkManifest:
    return replace(
        manifest, chunks=(replace(manifest.chunks[0], end_frame=31), *manifest.chunks[1:])
    )


@pytest.mark.parametrize(
    "mutation",
    [
        _unsupported_schema,
        _zero_total,
        _negative_start,
        _zero_length,
        _gap,
        _overlap,
        _duplicate_index,
        _exceeds_bound,
    ],
)
def test_manifest_rejects_malformed_contracts(
    mutation: Callable[[ChunkManifest], ChunkManifest],
) -> None:
    # Given a valid multi-chunk contract, when one invariant is corrupted, then a typed error is raised.
    manifest = plan_chunks(_plan([Fraction(1), Fraction(1)]), 30, ChunkPolicy(max_chunk_frames=30))
    with pytest.raises(ChunkManifestValidationError):
        _ = mutation(manifest)


def test_policy_rejects_unsafe_or_impossible_bounds() -> None:
    # Given malformed policy values, when constructed, then planning cannot begin.
    for kwargs in (
        {"max_chunk_frames": 0},
        {"max_chunk_seconds": 0},
        {"max_chunk_count": 0},
        {"audio_sample_rate": 0},
    ):
        with pytest.raises(ChunkManifestValidationError):
            _ = ChunkPolicy(**kwargs)


def test_plan_chunks_rejects_chunk_count_above_policy() -> None:
    # Given a render requiring more chunks than allowed, when planned, then fail with a typed error.
    with pytest.raises(ChunkManifestValidationError, match="chunk count"):
        _ = plan_chunks(
            _plan([Fraction(100)], fps=30),
            30,
            ChunkPolicy(max_chunk_seconds=10, max_chunk_count=2),
        )
