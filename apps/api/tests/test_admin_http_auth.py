# pyright: reportMissingImports=false, reportAttributeAccessIssue=false

from importlib import import_module

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shorts_api.auth import ApiKeyMiddleware

app = import_module("shorts_api.main").app
admin = import_module("shorts_api.routes.admin")


@pytest.mark.parametrize("key", ["local-admin-key-change-me", "<a-strong-random-string>"])
def test_admin_middleware_disables_public_example(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    # Given middleware alone: neither startup validation nor the router can mask its decision.
    monkeypatch.setenv("ADMIN_API_KEY", key)
    isolated = FastAPI()
    isolated.add_middleware(ApiKeyMiddleware)
    # When requesting an unknown admin path with the configured example.
    with TestClient(isolated) as client:
        response = client.get("/api/admin/nonexistent", headers={"X-Admin-Key": key})
    # Then the disabled-key response wins over routing (404).
    assert response.status_code == 401
    assert response.json() == {"detail": "Admin access denied"}


@pytest.mark.parametrize("environment, status", [("development", 403), ("production", 503)])
@pytest.mark.parametrize("key", ["local-admin-key-change-me", "<a-strong-random-string>"])
def test_admin_router_disables_public_example(
    monkeypatch: pytest.MonkeyPatch, environment: str, status: int, key: str,
) -> None:
    # Given the actual router without middleware or startup validation.
    monkeypatch.setenv("ADMIN_API_KEY", key)
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)
    isolated = FastAPI()
    isolated.include_router(admin.router, prefix="/api/admin")
    # When
    with TestClient(isolated) as client:
        response = client.get("/api/admin/health", headers={"X-Admin-Key": key})
    # Then
    assert response.status_code == status


def _mock_health_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "db": {"status": "up"},
        "redis": {"status": "up"},
        "uptime_seconds": 1,
    }


async def _mock_get_system_health() -> dict[str, object]:
    return _mock_health_payload()


def test_admin_endpoint_returns_401_without_admin_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("API_KEY", "global-secret")
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)

    with TestClient(app) as client:
        response = client.get("/api/admin/health")

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin access denied"


def test_admin_endpoint_returns_200_with_correct_admin_api_key(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("API_KEY", "global-secret")
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)

    with TestClient(app) as client:
        response = client.get(
            "/api/admin/health",
            headers={"x-admin-key": "admin-secret"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_global_api_key_alone_does_not_grant_admin_access(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setenv("API_KEY", "global-secret")
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)

    with TestClient(app) as client:
        response = client.get(
            "/api/admin/health",
            headers={"x-api-key": "global-secret"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin access denied"


def test_admin_endpoint_returns_403_with_wrong_admin_key(monkeypatch) -> None:
    """Middleware rejects invalid admin key with 403 before reaching router."""
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)

    with TestClient(app) as client:
        response = client.get(
            "/api/admin/health",
            headers={"x-admin-key": "wrong-key"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access denied"


def test_admin_endpoint_returns_401_when_admin_key_not_configured(monkeypatch) -> None:
    """Middleware rejects when ADMIN_API_KEY env var is empty/unset."""
    monkeypatch.setenv("ADMIN_API_KEY", "")
    monkeypatch.setattr(admin.admin_service, "get_system_health", _mock_get_system_health)

    with TestClient(app) as client:
        response = client.get(
            "/api/admin/health",
            headers={"x-admin-key": "any-key"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin access denied"


def test_admin_middleware_blocks_before_router_dependency(monkeypatch) -> None:
    """Verify middleware auth runs even for non-existent admin sub-paths.

    This proves defense-in-depth: a new admin sub-path that somehow bypasses
    the router dependency would still be caught by middleware.
    """
    monkeypatch.setenv("ADMIN_API_KEY", "admin-secret")

    with TestClient(app) as client:
        # No x-admin-key header → 401 from middleware
        response = client.get("/api/admin/nonexistent-endpoint")
    assert response.status_code == 401

    with TestClient(app) as client:
        # Wrong key → 403 from middleware
        response = client.get(
            "/api/admin/nonexistent-endpoint",
            headers={"x-admin-key": "bad"},
        )
    assert response.status_code == 403
