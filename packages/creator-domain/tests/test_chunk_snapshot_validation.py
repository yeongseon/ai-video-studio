from __future__ import annotations

import hashlib
import json
import random
from dataclasses import fields, replace
from typing import Final, TypedDict

import pytest
from pydantic import TypeAdapter

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
from creator_domain.models.chunk_manifest import ChunkSourceRef
from creator_domain.models.chunk_policy import ChunkEncodingProfile, ChunkOutputSpec

_SNAPSHOT_MUTATION_SEED: Final = 969_2026_11_08


class _FractionPayload(TypedDict):
    numerator: int
    denominator: int


class _SourceRefPayload(TypedDict):
    segment_index: int
    source: str
    asset_id: int
    content_revision: int
    start_frame: int
    end_frame: int


class _ChunkPayload(TypedDict):
    index: int
    start_frame: int
    end_frame: int
    start_s: _FractionPayload
    end_s: _FractionPayload
    start_sample: int
    end_sample: int
    source_refs: list[_SourceRefPayload]


class _OutputSpecPayload(TypedDict):
    width: int
    height: int
    fps: int


class _EncodingProfilePayload(TypedDict):
    name: str
    video_codec: str
    audio_codec: str
    crf: int
    preset: str


class _ApprovalPayload(TypedDict):
    timeline_id: str
    revision: int


class _PolicyPayload(TypedDict):
    max_chunk_frames: int
    max_chunk_count: int
    audio_sample_rate: int


class _ManifestPayload(TypedDict):
    schema_version: int
    approval: _ApprovalPayload
    total_frames: int
    fps: int
    output_spec: _OutputSpecPayload
    encoding_profile: _EncodingProfilePayload
    policy: _PolicyPayload
    chunks: list[_ChunkPayload]
    manifest_id: str


_PAYLOAD_ADAPTER: Final = TypeAdapter(_ManifestPayload)


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
            approval=ApprovalIdentity(timeline_id="timeline-snapshot", revision=1),
            source_assets=(SourceAssetIdentity(asset_id=1, content_revision=1),),
        ),
        30,
        ChunkPolicy(max_chunk_frames=60),
    )


def _rehash_payload(payload: _ManifestPayload) -> str:
    content = {key: value for key, value in payload.items() if key != "manifest_id"}
    payload["manifest_id"] = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return json.dumps(payload, sort_keys=True)


def _malformed_direct_manifest(manifest: ChunkManifest, field_name: str) -> ChunkManifest:
    if field_name == "width":
        return replace(manifest, output_spec=replace(manifest.output_spec, width=0))
    if field_name == "height":
        return replace(manifest, output_spec=replace(manifest.output_spec, height=-1))
    if field_name == "crf":
        return replace(manifest, encoding_profile=replace(manifest.encoding_profile, crf=99))
    if field_name == "name":
        return replace(
            manifest,
            encoding_profile=replace(manifest.encoding_profile, name="x" * 51),
        )
    source_ref = replace(manifest.chunks[0].source_refs[0], source="")
    return replace(manifest, chunks=(replace(manifest.chunks[0], source_refs=(source_ref,)),))


def _mutate_snapshot_payload(payload: _ManifestPayload, snapshot: str, field_name: str) -> None:
    invalid_values: dict[str, dict[str, int | str]] = {
        "output_spec": {"width": 0, "height": 0, "fps": 0},
        "encoding_profile": {
            "name": "x" * 51,
            "video_codec": "invalid",
            "audio_codec": "invalid",
            "crf": 52,
            "preset": "x" * 51,
        },
        "source_ref": {
            "segment_index": -1,
            "source": "",
            "asset_id": 0,
            "content_revision": -1,
            "start_frame": -1,
            "end_frame": 0,
        },
    }
    if snapshot == "source_ref":
        payload["chunks"][0]["source_refs"][0][field_name] = invalid_values[snapshot][field_name]
        return
    if snapshot == "output_spec":
        payload["output_spec"][field_name] = invalid_values[snapshot][field_name]
        return
    payload["encoding_profile"][field_name] = invalid_values[snapshot][field_name]


@pytest.mark.parametrize("field_name", ["width", "height", "crf", "name", "source"])
def test_snapshot_rejects_malformed_direct_construction(field_name: str) -> None:
    # Given one malformed serialized snapshot value inside an otherwise valid manifest.
    manifest = _manifest()

    # When constructed directly, then the snapshot boundary rejects it before hashing.
    with pytest.raises(ChunkManifestValidationError):
        _ = _malformed_direct_manifest(manifest, field_name)


@pytest.mark.parametrize("field_name", ["width", "height", "crf", "name", "source"])
def test_snapshot_rejects_freshly_rehashed_json(field_name: str) -> None:
    # Given malformed snapshot content carrying a fresh matching canonical hash.
    manifest = _manifest()
    payload = _PAYLOAD_ADAPTER.validate_json(manifest.to_json())
    snapshot = (
        "output_spec"
        if field_name in {"width", "height"}
        else "encoding_profile"
        if field_name in {"crf", "name"}
        else "source_ref"
    )
    _mutate_snapshot_payload(payload, snapshot, field_name)

    # When deserialized, then semantic validation wins over hash consistency.
    with pytest.raises(ChunkManifestValidationError):
        _ = ChunkManifest.from_json(_rehash_payload(payload))


def test_every_nested_snapshot_field_rejects_seeded_invalid_mutation() -> None:
    # Given one generated invalid mutation for every field in every nested snapshot type.
    manifest = _manifest()
    cases = [
        *(("output_spec", field.name) for field in fields(ChunkOutputSpec)),
        *(("encoding_profile", field.name) for field in fields(ChunkEncodingProfile)),
        *(("source_ref", field.name) for field in fields(ChunkSourceRef)),
    ]
    random.Random(_SNAPSHOT_MUTATION_SEED).shuffle(cases)

    # When each mutation is freshly hashed and loaded, then none survives validation.
    for snapshot, field_name in cases:
        payload = _PAYLOAD_ADAPTER.validate_json(manifest.to_json())
        _mutate_snapshot_payload(payload, snapshot, field_name)
        with pytest.raises(ChunkManifestValidationError):
            _ = ChunkManifest.from_json(_rehash_payload(payload))
