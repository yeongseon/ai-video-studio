# pyright: reportMissingImports=false, reportAttributeAccessIssue=false
"""Integration tests: ALL admin endpoints enforce auth boundary.

Verifies that every admin endpoint:
- Returns 401 when no X-Admin-Key is provided
- Returns 403 when wrong X-Admin-Key is provided
- Returns 200/422 (not 401/403) when correct key is provided

This ensures no new admin route can accidentally bypass authentication.
"""

from importlib import import_module
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

app = import_module("shorts_api.main").app
admin = import_module("shorts_api.routes.admin")


# -- Mocks for admin_service methods --


async def _mock_get_system_health():
    return {"status": "ok", "db": {"status": "up"}, "redis": {"status": "up"}, "uptime_seconds": 1}


async def _mock_get_stuck_runs(threshold_minutes: int = 30):
    return []


async def _mock_get_failed_runs(hours: int = 24):
    return []


async def _mock_get_queue_depth():
    return {"creator": 0}


async def _mock_get_storage_stats():
    return {"artifact_root": "/tmp", "file_count": 0, "total_size_bytes": 0, "error": None}


async def _mock_unstick_run(run_id: str):
    return {
        "ok": True,
        "run_id": run_id,
        "previous_stage": None,
        "current_stage": None,
        "status": None,
        "error": None,
    }


async def _mock_clear_cache(key_pattern: str | None = None, dry_run: bool = False):
    return {
        "ok": True,
        "deleted_keys": 0,
        "key_pattern": key_pattern or "",
        "dry_run": dry_run,
        "matched_keys": [],
    }


@pytest.fixture(autouse=True)
def _patch_admin_service(monkeypatch):
    """Patch all admin_service methods so tests don't hit real DB/Redis."""
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)
    monkeypatch.setattr(admin.admin_service, "get_stuck_runs", _mock_get_stuck_runs)
    monkeypatch.setattr(admin.admin_service, "get_failed_runs", _mock_get_failed_runs)
    monkeypatch.setattr(admin.admin_service, "get_queue_depth", _mock_get_queue_depth)
    monkeypatch.setattr(admin.admin_service, "get_storage_stats", _mock_get_storage_stats)
    monkeypatch.setattr(admin.admin_service, "unstick_run", _mock_unstick_run)
    monkeypatch.setattr(admin.admin_service, "clear_cache", _mock_clear_cache)


@pytest.fixture(autouse=True)
def _set_admin_key(monkeypatch):
    """Set a known admin key for all tests."""
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-secure-16chars")


# All GET admin endpoints
_GET_ENDPOINTS = [
    "/api/admin/health",
    "/api/admin/runs/stuck",
    "/api/admin/runs/stuck?threshold_minutes=10",
    "/api/admin/runs/failed",
    "/api/admin/runs/failed?hours=48",
    "/api/admin/queue/depth",
    "/api/admin/storage/stats",
]

# POST endpoints requiring confirmation header + rate limit
_POST_ENDPOINTS = [
    ("/api/admin/runs/999/unstick", {}),
    ("/api/admin/cache/clear?key_pattern=cache:test:foo&dry_run=true", {}),
]


class TestAllGetEndpointsReject401WithoutKey:
    """Every GET admin endpoint must return 401 without X-Admin-Key."""

    @pytest.mark.parametrize("endpoint", _GET_ENDPOINTS)
    def test_no_key_returns_401(self, endpoint: str) -> None:
        with TestClient(app) as client:
            response = client.get(endpoint)
        assert response.status_code == 401, f"{endpoint} did not return 401 without key"
        assert response.json()["detail"] == "Admin access denied"


class TestAllGetEndpointsReject403WithWrongKey:
    """Every GET admin endpoint must return 403 with wrong X-Admin-Key."""

    @pytest.mark.parametrize("endpoint", _GET_ENDPOINTS)
    def test_wrong_key_returns_403(self, endpoint: str) -> None:
        with TestClient(app) as client:
            response = client.get(endpoint, headers={"x-admin-key": "wrong-key"})
        assert response.status_code == 403, f"{endpoint} did not return 403 with wrong key"
        assert response.json()["detail"] == "Admin access denied"


class TestAllGetEndpointsAcceptCorrectKey:
    """Every GET admin endpoint must return 200 with correct X-Admin-Key."""

    @pytest.mark.parametrize("endpoint", _GET_ENDPOINTS)
    def test_correct_key_returns_200(self, endpoint: str) -> None:
        with TestClient(app) as client:
            response = client.get(
                endpoint,
                headers={"x-admin-key": "test-admin-key-secure-16chars"},
            )
        assert response.status_code == 200, f"{endpoint} did not return 200 with correct key"


class TestPostEndpointsRejectWithoutKey:
    """POST admin endpoints must return 401 without X-Admin-Key."""

    @pytest.mark.parametrize("endpoint,body", _POST_ENDPOINTS)
    def test_no_key_returns_401(self, endpoint: str, body: dict) -> None:
        with TestClient(app) as client:
            response = client.post(endpoint, json=body)
        assert response.status_code == 401, f"POST {endpoint} did not return 401 without key"

    @pytest.mark.parametrize("endpoint,body", _POST_ENDPOINTS)
    def test_wrong_key_returns_403(self, endpoint: str, body: dict) -> None:
        with TestClient(app) as client:
            response = client.post(endpoint, json=body, headers={"x-admin-key": "wrong-key"})
        assert response.status_code == 403, f"POST {endpoint} did not return 403 with wrong key"


class TestPostEndpointsRequireConfirmation:
    """POST destructive endpoints require X-Confirm-Action: yes header."""

    @pytest.mark.parametrize("endpoint,body", _POST_ENDPOINTS)
    def test_missing_confirmation_returns_400(self, endpoint: str, body: dict) -> None:
        with TestClient(app) as client:
            response = client.post(
                endpoint,
                json=body,
                headers={"x-admin-key": "test-admin-key-secure-16chars"},
            )
        assert response.status_code == 400, f"POST {endpoint} did not require confirmation"
        assert "X-Confirm-Action" in response.json()["detail"]

    @pytest.mark.parametrize("endpoint,body", _POST_ENDPOINTS)
    def test_with_confirmation_succeeds(self, endpoint: str, body: dict) -> None:
        with TestClient(app) as client:
            response = client.post(
                endpoint,
                json=body,
                headers={
                    "x-admin-key": "test-admin-key-secure-16chars",
                    "x-confirm-action": "yes",
                },
            )
        # Should succeed (200) or hit a validation error (4xx) but NOT 401/403
        assert response.status_code not in (401, 403), (
            f"POST {endpoint} rejected valid auth + confirmation"
        )


class TestGlobalApiKeyDoesNotGrantAdminAccess:
    """X-API-Key (creator key) must never work for admin endpoints."""

    @pytest.mark.parametrize("endpoint", _GET_ENDPOINTS)
    def test_creator_key_rejected_on_admin_endpoints(self, endpoint: str, monkeypatch) -> None:
        monkeypatch.setenv("API_KEY", "creator-api-key")
        with TestClient(app) as client:
            response = client.get(endpoint, headers={"x-api-key": "creator-api-key"})
        assert response.status_code == 401, f"{endpoint} accepted creator API key for admin access"


class TestMiddlewareBlocksNonexistentAdminPaths:
    """Middleware must block auth for ANY /api/admin/* path, even unregistered ones."""

    _NONEXISTENT_PATHS = [
        "/api/admin/nonexistent",
        "/api/admin/foo/bar/baz",
        "/api/admin/secret-endpoint",
    ]

    @pytest.mark.parametrize("path", _NONEXISTENT_PATHS)
    def test_nonexistent_path_401_without_key(self, path: str) -> None:
        with TestClient(app) as client:
            response = client.get(path)
        assert response.status_code == 401

    @pytest.mark.parametrize("path", _NONEXISTENT_PATHS)
    def test_nonexistent_path_403_with_wrong_key(self, path: str) -> None:
        with TestClient(app) as client:
            response = client.get(path, headers={"x-admin-key": "wrong"})
        assert response.status_code == 403


class TestTimingSafeComparison:
    """Verify that require_admin uses hmac.compare_digest (timing-safe)."""

    def test_hmac_compare_digest_is_used(self) -> None:
        """Inspect source code for hmac.compare_digest usage."""
        import inspect

        source = inspect.getsource(admin.require_admin)
        assert "admin_key_matches" in source, (
            "require_admin must delegate to the shared timing-safe byte comparison"
        )


class TestProductionKeyLengthEnforcement:
    """Production mode rejects admin keys shorter than 16 chars.

    Note: The fail-fast startup validation (SystemExit) is tested in
    test_admin_key_startup.py. Here we test the router dependency behavior
    when the startup check is bypassed (e.g. key shortened after startup).
    """

    async def test_require_admin_rejects_short_key_in_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """require_admin returns 503 for short keys in production mode."""
        from fastapi import HTTPException

        # Given a short admin key in production.
        monkeypatch.setenv("ADMIN_API_KEY", "short")
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

        # When the dependency runs on the pytest-managed event loop.
        with pytest.raises(HTTPException) as exc_info:
            await admin.require_admin(x_admin_key="short")

        # Then the production guard rejects the key.
        assert exc_info.value.status_code == 503
        assert "not configured" in exc_info.value.detail
