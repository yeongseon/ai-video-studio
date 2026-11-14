from __future__ import annotations

from dataclasses import dataclass

from creator_domain.exceptions import ChunkManifestValidationError

from .render_plan import RenderPlan


@dataclass(frozen=True, slots=True)
class ApprovalIdentity:
    timeline_id: str
    revision: int

    def validate(self) -> None:
        if not self.timeline_id or self.revision < 1:
            raise ChunkManifestValidationError("approval identity is missing or malformed")


@dataclass(frozen=True, slots=True)
class SourceAssetIdentity:
    asset_id: int
    content_revision: int

    def validate(self) -> None:
        if self.asset_id < 1 or self.content_revision < 0:
            raise ChunkManifestValidationError("source asset identity is missing or malformed")


@dataclass(frozen=True, slots=True)
class ChunkPlanningInput:
    render_plan: RenderPlan
    approval: ApprovalIdentity
    source_assets: tuple[SourceAssetIdentity, ...]

    def validate(self) -> None:
        self.approval.validate()
        if len(self.source_assets) != len(self.render_plan.segments):
            raise ChunkManifestValidationError(
                "source asset identity must exist for every render segment"
            )
        for source_asset in self.source_assets:
            source_asset.validate()
