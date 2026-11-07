from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from creator_domain.exceptions import ChunkManifestValidationError
from creator_domain.models import (
    ApprovalIdentity,
    ChunkManifest,
    ChunkPlanningInput,
    ChunkPolicy,
    EncodingProfile,
    OutputSpec,
    RenderPlan,
    RenderSegment,
    RenderSegmentKind,
    SourceAssetIdentity,
    plan_chunks,
)


def _manifest() -> ChunkManifest:
    plan = RenderPlan(
        segments=[
            RenderSegment(
                kind=RenderSegmentKind.IMAGE,
                source="assets/scene.png",
                timeline_start_seconds=0.0,
                duration_seconds=2.0,
            )
        ],
        output_spec=OutputSpec(width=1920, height=1080, fps=30),
        encoding_profile=EncodingProfile.standard(),
    )
    return plan_chunks(
        ChunkPlanningInput(
            render_plan=plan,
            approval=ApprovalIdentity(timeline_id="timeline-validation", revision=1),
            source_assets=(SourceAssetIdentity(asset_id=1, content_revision=1),),
        ),
        30,
        ChunkPolicy(max_chunk_frames=60),
    )


def _canonical_without_id(serialized: str, manifest_id: str) -> bytes:
    compact = serialized.replace(" ", "")
    return compact.replace(f'"manifest_id":"{manifest_id}",', "").encode()


def test_manifest_rejects_source_ref_outside_chunk_on_construction() -> None:
    # Given a source reference with an invalid index and range outside its chunk.
    manifest = _manifest()
    source_ref = replace(
        manifest.chunks[0].source_refs[0],
        segment_index=-7,
        start_frame=-100,
        end_frame=999,
    )

    # When the malformed manifest is constructed, then typed validation rejects it.
    with pytest.raises(ChunkManifestValidationError, match="source"):
        _ = replace(
            manifest,
            chunks=(replace(manifest.chunks[0], source_refs=(source_ref,)),),
        )


def test_manifest_rejects_source_ref_outside_chunk_from_json() -> None:
    # Given canonical serialized content with the same invalid source geometry.
    manifest = _manifest()
    malformed = (
        manifest.to_json()
        .replace('"end_frame": 60, "segment_index": 0', '"end_frame": 999, "segment_index": -7')
        .replace('"start_frame": 0}', '"start_frame": -100}')
    )
    fresh_id = hashlib.sha256(_canonical_without_id(malformed, manifest.manifest_id)).hexdigest()
    payload = malformed.replace(manifest.manifest_id, fresh_id)

    # When parsed with a matching fresh hash, then model validation still rejects it.
    with pytest.raises(ChunkManifestValidationError, match="source"):
        _ = ChunkManifest.from_json(payload)


def test_manifest_rejects_output_fps_mismatch_on_construction() -> None:
    # Given a 30-fps manifest whose duplicated output contract says 60 fps.
    manifest = _manifest()

    # When the malformed manifest is constructed, then typed validation rejects it.
    with pytest.raises(ChunkManifestValidationError, match="fps"):
        _ = replace(manifest, output_spec=replace(manifest.output_spec, fps=60))


def test_manifest_rejects_output_fps_mismatch_from_json() -> None:
    # Given canonical serialized content with a mismatched output fps and fresh hash.
    manifest = _manifest()
    malformed = (
        manifest.to_json()
        .replace('"output_spec": {"fps": 30', '"output_spec": {"fps": 60')
    )
    fresh_id = hashlib.sha256(_canonical_without_id(malformed, manifest.manifest_id)).hexdigest()
    payload = malformed.replace(manifest.manifest_id, fresh_id)

    # When parsed, then the same typed model validation rejects it.
    with pytest.raises(ChunkManifestValidationError, match="fps"):
        _ = ChunkManifest.from_json(payload)


@pytest.mark.parametrize("second_start", [29, 31], ids=["overlap", "gap"])
def test_manifest_rejects_source_ref_coverage_discontinuity(second_start: int) -> None:
    # Given ordered references that overlap or leave a gap within one chunk.
    manifest = _manifest()
    original = manifest.chunks[0].source_refs[0]
    refs = (
        replace(original, end_frame=30),
        replace(original, start_frame=second_start, segment_index=1),
    )

    # When constructed, then source ranges cannot cover any frame twice or not at all.
    with pytest.raises(ChunkManifestValidationError, match="coverage"):
        _ = replace(manifest, chunks=(replace(manifest.chunks[0], source_refs=refs),))


def test_manifest_rejects_nonpositive_resolved_audio_sample_rate() -> None:
    # Given a resolved policy and sample geometry that consistently claim zero samples.
    manifest = _manifest()
    chunk = replace(manifest.chunks[0], start_sample=0, end_sample=0)
    policy = replace(manifest.policy, audio_sample_rate=0)

    # When constructed, then the serialized resolved policy cannot bypass policy bounds.
    with pytest.raises(ChunkManifestValidationError, match="sample rate"):
        _ = replace(manifest, policy=policy, chunks=(chunk,))
