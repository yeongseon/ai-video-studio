from __future__ import annotations

from pathlib import Path, PurePosixPath

from creator_domain.models import MediaAsset, RenderPlan
from creator_service.object_storage import ArtifactStorageBackend
from creator_service.render_scratch_monitor import current_budget


class RenderSourceError(RuntimeError):
    pass


def materialize_plan(
    plan: RenderPlan, assets: list[MediaAsset], destination: Path,
    *, backend: ArtifactStorageBackend, storage_provider: str,
) -> RenderPlan:
    """Only compiler-authorized object keys enter storage; never fetch asset URLs."""
    by_key = {asset.storage_key: asset for asset in assets}
    sources: dict[str, str] = {}
    for segment in plan.segments:
        key = segment.source
        asset = by_key.get(key)
        if asset is None:
            raise RenderSourceError("Render source is not an authorized asset")
        parts = key.split("/")
        if (
            PurePosixPath(key).is_absolute() or any(p in {"", ".", ".."} for p in parts)
            or any(c in key for c in ("\\", ":", "\x00"))
            or not key.startswith(f"workspaces/{asset.workspace_id}/assets/")
        ):
            raise RenderSourceError("Unsafe render storage key")
        if asset.metadata.get("storage_provider", storage_provider) != storage_provider:
            raise RenderSourceError("Render storage provider does not match configured backend")
        if key not in sources:
            data = backend.download_bytes(key)
            if not data:
                raise RenderSourceError("Render source is empty")
            path = destination / f"source_{len(sources):04d}"
            path.write_bytes(data)
            budget = current_budget.get()
            if budget is not None:
                budget.add_baseline(len(data))
            sources[key] = str(path)
    return plan.model_copy(update={
        "segments": [segment.model_copy(update={"source": sources[segment.source]})
                     for segment in plan.segments],
    })
