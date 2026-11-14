"""Admin API endpoints for operational control and observability.

Auth boundary:
- ``/api/admin/*`` routes authenticate with ``X-Admin-Key``.
- ``/api/creator/*`` routes authenticate with ``X-API-Key`` or ``Authorization: Bearer``.
- These are separate auth domains and credentials are not interchangeable.

This module exposes backend-only admin endpoints under ``/api/admin``.
It does not implement any admin dashboard UI; frontend dashboard work is
handled separately from this API surface.
"""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from functools import partial
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from shorts_api.admin_rate_limit import DestructiveOpRateLimiter as DestructiveOpRateLimiter, RedisRateLimiter as RedisRateLimiter

from creator_service.admin_service import admin_service
from creator_service.blocking_io import run_admin_limiter
from shorts_api.lifecycle import admin_key_matches, configured_admin_api_key

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("admin.audit")

_ALLOWED_CACHE_CLEAR_PREFIXES = (
    "cache:creator:",
    "cache:",
    "cache:test:",
    "session:",
    "model_health:",
    "rate_limit:",
    "artifact:",
)

_DANGEROUS_PATTERNS = frozenset({"*", "*:*", ""})


# Global rate limiter instance
_rate_limiter = RedisRateLimiter(max_ops=10, window_seconds=60)


async def require_admin(x_admin_key: str | None = Header(default=None)) -> str:
    if not x_admin_key:
        raise HTTPException(status_code=401, detail="Admin access denied")

    expected = configured_admin_api_key()
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    if environment == "production" and (not expected or len(expected) < 16):
        logger.error("ADMIN_API_KEY is not set or too short (min 16 chars)")
        raise HTTPException(status_code=503, detail="Admin API not configured")
    if not expected or not admin_key_matches(x_admin_key, expected):
        raise HTTPException(status_code=403, detail="Admin access denied")
    return x_admin_key


async def require_confirmation_and_rate_limit(
    x_confirm_action: str = Header(None), x_admin_key: str = Depends(require_admin)
) -> None:
    """Middleware to ensure destructive operations have confirmation header and respect rate limits."""
    # Check confirmation header
    if not x_confirm_action or x_confirm_action.lower() != "yes":
        raise HTTPException(
            status_code=400,
            detail="Destructive operation requires X-Confirm-Action: yes header",
        )

    # Check rate limit
    allowed = await run_admin_limiter(partial(_rate_limiter.is_allowed, "admin:destructive", x_admin_key))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: maximum 10 destructive operations per minute",
        )


router = APIRouter(
    prefix="",
    tags=["admin-api"],
    dependencies=[Depends(require_admin)],
)

from shorts_api.schemas.admin import (
    CacheClearResponse,
    HealthResponse,
    QueueDepthResponse,
    RunInfo,
    StorageStatsResponse,
    UnstickRunResponse,
)

@router.get("/health", response_model=HealthResponse)
async def admin_health() -> dict[str, Any]:
    return await admin_service.get_system_health()


@router.get("/runs/stuck", response_model=list[RunInfo])
async def admin_stuck_runs(
    threshold_minutes: int = Query(default=30, ge=1),
) -> list[dict[str, Any]]:
    return await admin_service.get_stuck_runs(threshold_minutes=threshold_minutes)


@router.get("/runs/failed", response_model=list[RunInfo])
async def admin_failed_runs(hours: int = Query(default=24, ge=1)) -> list[dict[str, Any]]:
    return await admin_service.get_failed_runs(hours=hours)


@router.get("/queue/depth", response_model=QueueDepthResponse)
async def admin_queue_depth() -> dict[str, int]:
    return await admin_service.get_queue_depth()


@router.get("/storage/stats", response_model=StorageStatsResponse)
async def admin_storage_stats() -> dict[str, Any]:
    return await admin_service.get_storage_stats()


@router.post("/runs/{run_id}/unstick", response_model=UnstickRunResponse)
async def admin_unstick_run(
    run_id: str,
    request: Request,
    admin_key: str = Depends(require_admin),
    _: None = Depends(require_confirmation_and_rate_limit),
) -> dict[str, Any]:
    key_fingerprint = (
        hashlib.sha256(admin_key.encode()).hexdigest()[:8]
        if isinstance(admin_key, str)
        else "unknown"
    )
    request_id = request.headers.get("X-Request-Id", "none")
    source_ip = request.client.host if request.client else "unknown"
    logger.warning("Admin mutation requested: unstick run_id=%s", run_id)
    audit_id = str(uuid.uuid4())
    audit_logger.warning(
        "ADMIN_ACTION: unstick_run | run_id=%s | key_fingerprint=%s | request_id=%s | source_ip=%s | audit_id=%s",
        run_id,
        key_fingerprint,
        request_id,
        source_ip,
        audit_id,
    )
    result = await admin_service.unstick_run(run_id)
    result["audit_id"] = audit_id
    return result


@router.post("/cache/clear", response_model=CacheClearResponse)
async def admin_clear_cache(
    request: Request,
    key_pattern: str | None = Query(default=None),
    dry_run: bool = Query(default=False),
    admin_key: str = Depends(require_admin),
    _: None = Depends(require_confirmation_and_rate_limit),
) -> dict[str, Any]:
    safe_pattern = key_pattern.replace("\n", "").replace("\r", "")[:200] if key_pattern else None
    key_fingerprint = (
        hashlib.sha256(admin_key.encode()).hexdigest()[:8]
        if isinstance(admin_key, str)
        else "unknown"
    )
    request_id = request.headers.get("X-Request-Id", "none")
    source_ip = request.client.host if request.client else "unknown"
    logger.warning(
        "Admin mutation requested: clear cache key_pattern=%s dry_run=%s",
        safe_pattern,
        dry_run,
    )
    audit_id = str(uuid.uuid4())
    if key_pattern is None:
        audit_logger.warning(
            "ADMIN_ACTION: cache_clear | dry_run=%s | key_fingerprint=%s | request_id=%s | source_ip=%s | audit_id=%s",
            dry_run,
            key_fingerprint,
            request_id,
            source_ip,
            audit_id,
        )
    else:
        audit_logger.warning(
            "ADMIN_ACTION: cache_clear | key_pattern=%s | dry_run=%s | key_fingerprint=%s | request_id=%s | source_ip=%s | audit_id=%s",
            safe_pattern,
            dry_run,
            key_fingerprint,
            request_id,
            source_ip,
            audit_id,
        )

    # Validate pattern against allowlist (dry_run=True allows any pattern for inspection)
    if not dry_run:
        if key_pattern is None or key_pattern.strip() in _DANGEROUS_PATTERNS:
            audit_logger.warning(
                "ADMIN_REJECTED: cache_clear | reason=dangerous_pattern | pattern=%s | key_fingerprint=%s | audit_id=%s",
                safe_pattern,
                key_fingerprint,
                audit_id,
            )
            raise HTTPException(
                status_code=400,
                detail="Destructive pattern rejected. Use a specific prefix from allowlist: "
                + ", ".join(_ALLOWED_CACHE_CLEAR_PREFIXES),
            )

        pattern_allowed = any(
            key_pattern.startswith(prefix) for prefix in _ALLOWED_CACHE_CLEAR_PREFIXES
        )
        if not pattern_allowed:
            audit_logger.warning(
                "ADMIN_REJECTED: cache_clear | reason=prefix_not_allowed | pattern=%s | key_fingerprint=%s | audit_id=%s",
                safe_pattern,
                key_fingerprint,
                audit_id,
            )
            raise HTTPException(
                status_code=400,
                detail="Pattern prefix not in allowlist. Allowed: "
                + ", ".join(_ALLOWED_CACHE_CLEAR_PREFIXES),
            )

    result = await admin_service.clear_cache(key_pattern=key_pattern, dry_run=dry_run)
    result["audit_id"] = audit_id
    return result
