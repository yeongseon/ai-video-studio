from __future__ import annotations

from dataclasses import replace

import pytest

from creator_domain.exceptions import ChunkManifestValidationError
from creator_domain.models import (
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


def _render_plan() -> RenderPlan:
    return RenderPlan(
        segments=[
            RenderSegment(
                kind=RenderSegmentKind.IMAGE,
                source="assets/scene.png",
                timeline_start_seconds=0.0,
                duration_seconds=1.0,
            )
        ],
        output_spec=OutputSpec(width=1920, height=1080, fps=30),
        encoding_profile=EncodingProfile.standard(),
    )


def _planning_input() -> ChunkPlanningInput:
    return ChunkPlanningInput(
        render_plan=_render_plan(),
        approval=ApprovalIdentity(timeline_id="timeline-7", revision=3),
        source_assets=(SourceAssetIdentity(asset_id=41, content_revision=2),),
    )


def test_manifest_id_tracks_approval_revision() -> None:
    # Given the same resolved plan approved at two immutable revisions.
    planning_input = _planning_input()
    revised = replace(
        planning_input,
        approval=replace(planning_input.approval, revision=4),
    )

    # When manifests are planned, then approval identity changes canonical identity.
    assert plan_chunks(planning_input, 30).manifest_id != plan_chunks(revised, 30).manifest_id


@pytest.mark.parametrize(
    "source_asset",
    [
        SourceAssetIdentity(asset_id=42, content_revision=2),
        SourceAssetIdentity(asset_id=41, content_revision=3),
    ],
    ids=["asset-id", "content-revision"],
)
def test_manifest_id_tracks_asset_content_identity(source_asset: SourceAssetIdentity) -> None:
    # Given the same source location resolved to a different asset identity or revision.
    planning_input = _planning_input()
    changed = replace(planning_input, source_assets=(source_asset,))

    # When manifests are planned, then source content identity changes the hash.
    assert plan_chunks(planning_input, 30).manifest_id != plan_chunks(changed, 30).manifest_id


@pytest.mark.parametrize(
    ("approval", "source_assets"),
    [
        (ApprovalIdentity(timeline_id="", revision=3), (SourceAssetIdentity(41, 2),)),
        (ApprovalIdentity(timeline_id="timeline-7", revision=0), (SourceAssetIdentity(41, 2),)),
        (ApprovalIdentity(timeline_id="timeline-7", revision=3), ()),
        (ApprovalIdentity(timeline_id="timeline-7", revision=3), (SourceAssetIdentity(0, 2),)),
        (ApprovalIdentity(timeline_id="timeline-7", revision=3), (SourceAssetIdentity(41, -1),)),
    ],
)
def test_planner_rejects_missing_or_malformed_immutable_identity(
    approval: ApprovalIdentity,
    source_assets: tuple[SourceAssetIdentity, ...],
) -> None:
    # Given missing or malformed approval and asset identity input.
    planning_input = ChunkPlanningInput(
        render_plan=_render_plan(),
        approval=approval,
        source_assets=source_assets,
    )

    # When planning begins, then the domain rejects the unprovable identity.
    with pytest.raises(ChunkManifestValidationError, match="identity"):
        _ = plan_chunks(planning_input, 30)


def test_planner_rejects_render_plan_without_identity() -> None:
    # Given a render plan without its approval and asset identity envelope.
    planning_input: ChunkPlanningInput | RenderPlan = _render_plan()

    # When planning begins, then missing identity is a typed validation error.
    with pytest.raises(ChunkManifestValidationError, match="identity"):
        _ = plan_chunks(planning_input, 30)


def test_serialized_manifest_rejects_malformed_asset_identity() -> None:
    # Given canonical JSON whose source asset identity is corrupted.
    manifest = plan_chunks(_planning_input(), 30)
    payload = manifest.to_json().replace('"asset_id": 41', '"asset_id": 0')

    # When parsed, then malformed immutable identity is rejected before hash comparison.
    with pytest.raises(ChunkManifestValidationError, match="identity"):
        _ = type(manifest).from_json(payload)
