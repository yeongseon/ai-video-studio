"""Routes for the per-project editable Timeline (SF-28).

One authoritative Timeline per Project. GET loads it (404 if absent);
PUT persists it with an atomic revision check (409 on stale write) and
cross-asset validation (400 on unavailable asset). Access is gated by
require_project_access, which returns 404 for cross-tenant access.
"""

import logging
import os

from creator_domain.models import EncodingProfile, OutputSpec, Timeline
from creator_domain.models.project import Project
from creator_service.media_asset_service import media_asset_service
from creator_service.object_storage import get_storage_backend
from creator_service.object_storage_reads import StorageObjectMissing, validate_key
from creator_service.timeline_compiler import compile_timeline_to_render_plan
from creator_service.timeline_service import timeline_service
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.responses import Response

from shorts_api.auth import CurrentUser, require_project_access
from shorts_api.media_streaming import stream_media

router = APIRouter(prefix="/projects", tags=["timeline"])


class SaveTimelineRequest(BaseModel):
    expected_revision: int = Field(ge=0)
    timeline: Timeline


@router.get("/{project_id}/timeline")
async def get_project_timeline(
    project_id: int,
    access: tuple[CurrentUser, Project] = Depends(require_project_access),
) -> dict[str, object]:
    user, _project = access
    timeline = await timeline_service.load_timeline(
        project_id=project_id, workspace_id=user.workspace_id
    )
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    return timeline.model_dump(mode="json")


@router.put("/{project_id}/timeline")
async def save_project_timeline(
    project_id: int,
    request: SaveTimelineRequest,
    access: tuple[CurrentUser, Project] = Depends(require_project_access),
) -> dict[str, object]:
    user, _project = access
    saved = await timeline_service.save_timeline(
        project_id=project_id,
        workspace_id=user.workspace_id,
        timeline=request.timeline,
        expected_revision=request.expected_revision,
    )
    return saved.model_dump(mode="json")


@router.get("/{project_id}/timeline/preview")
async def preview_project_timeline(
    project_id: int,
    output: str = Query(default="short_vertical"),
    encoding: str = Query(default="preview"),
    access: tuple[CurrentUser, Project] = Depends(require_project_access),
) -> dict[str, object]:
    """Compile the saved Timeline into the same RenderPlan final rendering consumes.

    The browser preview and the renderer share one compiled source, so what the
    user previews is what gets rendered. Unknown presets -> 400; absent timeline
    -> 404; unavailable media (compiler ValidationError) -> 400 via the handler.
    """
    user, _project = access
    timeline = await timeline_service.load_timeline(
        project_id=project_id, workspace_id=user.workspace_id
    )
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")

    try:
        output_spec = OutputSpec.preset(output)
        encoding_profile = EncodingProfile.by_name(encoding)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    plan = await compile_timeline_to_render_plan(
        timeline,
        workspace_id=user.workspace_id,
        output_spec=output_spec,
        encoding_profile=encoding_profile,
        asset_resolver=media_asset_service,
    )
    payload = plan.model_dump(mode="json")
    ordered = sorted(timeline.segments, key=lambda segment: segment.timeline_start_seconds)
    for segment, source in zip(payload["segments"], ordered, strict=True):
        segment["media_url"] = f"/api/creator/projects/{project_id}/assets/{source.asset_id}/content"
    payload["timeline_revision"] = timeline.revision
    return payload


@router.head("/{project_id}/assets/{asset_id}/content", include_in_schema=False)
@router.get("/{project_id}/assets/{asset_id}/content")
async def get_project_asset_content(
    project_id: int,
    asset_id: int,
    request: Request,
    access: tuple[CurrentUser, Project] = Depends(require_project_access),
) -> Response:
    user, _project = access
    asset = await media_asset_service.get_asset(asset_id, user.workspace_id)
    if asset is None or asset.project_id != project_id or not asset.storage_key:
        raise HTTPException(status_code=404, detail="Asset not found")
    try:
        components = validate_key(asset.storage_key)
    except StorageObjectMissing:
        raise HTTPException(404, "Asset not found") from None
    if components[0] == "workspaces" and (len(components) < 3 or components[1] != str(user.workspace_id)):
        raise HTTPException(404, "Asset not found")
    provider = asset.metadata.get("storage_provider", "local")
    if provider != os.getenv("STORAGE_BACKEND", "local"):
        raise HTTPException(status_code=409, detail="Remote media preview is not available")
    if provider not in {"local", "s3", "azure_blob"}:
        raise HTTPException(status_code=409, detail="Media storage provider is unsupported")
    allowed = {"image/png", "image/jpeg", "image/webp", "video/mp4", "video/webm"}
    if asset.mime_type not in allowed:
        raise HTTPException(status_code=415, detail="Media cannot be previewed")
    from creator_service.blocking_io import run_blocking

    try:
        backend = await run_blocking(get_storage_backend)
    except NotImplementedError:
        raise HTTPException(409, "Media storage provider is unsupported") from None
    except Exception:  # noqa: BLE001 - lazy provider SDK/configuration boundary
        logging.getLogger(__name__).error("Media storage configuration unavailable")
        raise HTTPException(502, "Media storage unavailable") from None
    if backend.provider != provider:
        raise HTTPException(409, "Remote media preview is not available")
    return await stream_media(backend, asset.storage_key, request, asset.mime_type)
