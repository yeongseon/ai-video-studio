import mimetypes
import os
from importlib import import_module

from creator_domain.sanitize import UnsafePathComponent, sanitize_path_component
from creator_service.artifact_download_service import read_artifact_bytes
from creator_service.db import get_pool
from creator_service.model_health_service import ModelHealthService, ModelStatus
from fastapi import Depends, FastAPI, HTTPException, Request
from redis.asyncio import Redis
from starlette import status
from starlette.responses import Response

from shorts_api.auth import CurrentUser, get_current_user
from shorts_api.lifecycle import REDIS_URL, admin_key_matches, configured_admin_api_key, shutdown_state

model_health = ModelHealthService()


def _resolve_model_health() -> ModelHealthService:
    try:
        main_module = import_module("shorts_api.main")

        return getattr(main_module, "model_health", model_health)
    except Exception:
        return model_health


def _resolve_read_artifact_bytes():
    try:
        main_module = import_module("shorts_api.main")

        return getattr(main_module, "read_artifact_bytes", read_artifact_bytes)
    except Exception:
        return read_artifact_bytes


def _resolve_get_pool():
    try:
        main_module = import_module("shorts_api.main")

        return getattr(main_module, "get_pool", get_pool)
    except Exception:
        return get_pool


def _resolve_redis_from_url():
    try:
        main_module = import_module("shorts_api.main")

        return getattr(main_module.Redis, "from_url", Redis.from_url)
    except Exception:
        return Redis.from_url


async def _validate_artifact_access(run_id: int, user: CurrentUser) -> None:
    """Validate that the user has access to the run's artifacts.
    
    Raises 404 if the run doesn't exist or belongs to a different workspace (anti-enumeration).
    """
    from shorts_api.auth import check_run_ownership
    
    # This will raise HTTPException(404) if access is denied
    await check_run_ownership(run_id, user.workspace_id, user.user_id)


async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _is_admin_request(request: Request) -> bool:
    """Check if request carries a valid admin key."""
    admin_key = configured_admin_api_key()
    if not admin_key:
        return False
    provided = request.headers.get("X-Admin-Key", "")
    return admin_key_matches(provided, admin_key)

async def health(request: Request) -> dict[str, object]:
    environment = os.getenv("ENVIRONMENT", "development").lower()
    is_admin = _is_admin_request(request)
    if environment in {"production", "staging"}:
        db_ok = False
        redis_ok = False

        try:
            pool = await _resolve_get_pool()()
            value = await pool.fetchval("SELECT 1")
            db_ok = value == 1
        except Exception:
            pass

        redis_client = _resolve_redis_from_url()(REDIS_URL)
        try:
            redis_pong = await redis_client.ping()
            redis_ok = bool(redis_pong)
        except Exception:
            pass
        finally:
            await redis_client.aclose()

        overall_ok = db_ok and redis_ok and not shutdown_state.is_shutting_down

        response_payload: dict[str, object] = {
            "status": "ok" if overall_ok else "unavailable",
        }
        if is_admin:
            response_payload["checks"] = {
                "database": {"status": "ok" if db_ok else "down"},
                "redis": {"status": "ok" if redis_ok else "down"},
            }
            response_payload["shutdown"] = shutdown_state.is_shutting_down

        if not overall_ok:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=response_payload,
            )
        return response_payload

    results = await _resolve_model_health().check_all()
    definitive = [r for r in results if r.status not in (ModelStatus.UNKNOWN,)]
    all_healthy = len(definitive) > 0 and all(
        r.status in (ModelStatus.HEALTHY, ModelStatus.CONFIGURED) for r in definitive
    )
    payload: dict[str, object] = {"status": "ok" if all_healthy else "degraded"}
    if is_admin:
        payload["models"] = {
            r.model_name: {
                "status": r.status.value,
                "response_time_ms": r.response_time_ms,
            }
            for r in results
        }
    return payload


async def serve_artifact(artifact_path: str, user: CurrentUser = Depends(get_current_user)):
    environment = os.getenv("ENVIRONMENT", "development").lower()
    if environment in ("production", "staging"):
        raise HTTPException(status_code=404, detail="Not found")

    path_components = artifact_path.split("/")
    if not artifact_path or any(component in {"", ".", ".."} for component in path_components):
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    # Extract and validate run_id from path (format: {run_id}/... where run_id should be numeric)
    try:
        run_id_str = path_components[0]
        run_id = int(run_id_str)
        # Validate user has access to this run's artifacts
        await _validate_artifact_access(run_id, user)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        safe_components = [
            sanitize_path_component(component, label=f"artifact_path[{index}]")
            for index, component in enumerate(path_components)
        ]
    except UnsafePathComponent as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    storage_key = "/".join(safe_components)
    try:
        content = _resolve_read_artifact_bytes()(storage_key)
        media_type, _ = mimetypes.guess_type(storage_key)
        return Response(
            content=content,
            media_type=media_type or "application/octet-stream",
            headers={"Warning": '299 - "Deprecated endpoint: use artifact-id download API"'},
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Artifact read failed") from exc


async def serve_local_artifact_file(path: str, user: CurrentUser = Depends(get_current_user)) -> Response:
    environment = os.getenv("ENVIRONMENT", "development").lower()
    if environment in ("production", "staging"):
        raise HTTPException(status_code=404, detail="Not found")

    path_components = path.split("/")
    if (
        not path
        or path.startswith("/")
        or any(component in {"", ".", ".."} for component in path_components)
    ):
        raise HTTPException(status_code=400, detail="Invalid artifact path")

    # Extract and validate run_id from path (format: {run_id}/... where run_id should be numeric)
    try:
        run_id_str = path_components[0]
        run_id = int(run_id_str)
        # Validate user has access to this run's artifacts
        await _validate_artifact_access(run_id, user)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")

    try:
        safe_components = [
            sanitize_path_component(component, label=f"path[{index}]")
            for index, component in enumerate(path_components)
        ]
    except UnsafePathComponent as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    storage_key = "/".join(safe_components)

    try:
        content = _resolve_read_artifact_bytes()(storage_key)
        media_type, _ = mimetypes.guess_type(storage_key)
        return Response(content=content, media_type=media_type or "application/octet-stream")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Artifact read failed") from exc


def register_health_routes(app: FastAPI) -> None:
    app.get("/healthz")(healthz)
    app.get("/health")(health)
    app.get("/artifacts/{artifact_path:path}", deprecated=True)(serve_artifact)
    app.get("/api/artifacts/files/{path:path}")(serve_local_artifact_file)
