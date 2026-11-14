"""Example recognition must not change genuine admin credentials."""

from unittest.mock import AsyncMock
from typing import Literal, assert_never

import pytest
from creator_service.production_checks import ProductionConfigError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shorts_api.auth import ApiKeyMiddleware
from shorts_api.health import register_health_routes
from shorts_api.lifecycle import configured_admin_api_key, validate_admin_api_key
from shorts_api.routes.admin import router


@pytest.fixture(params=[
    " local-admin-key-change-me", "local-admin-key-change-me ",
    "\tlocal-admin-key-change-me\t", "local-admin-key-change-me\r\n",
    "'local-admin-key-change-me'", '"local-admin-key-change-me"',
    "LOCAL-ADMIN-KEY-CHANGE-ME", "Local-Admin-Key-Change-Me",
    ' \t" local-admin-key-change-me\t"\r\n',
    " <A-STRONG-RANDOM-STRING> ", "'<a-strong-random-string>'",
    '"<a-strong-random-string>"',
])
def example_key(request: pytest.FixtureRequest) -> str:
    assert isinstance(request.param, str)
    return request.param


@pytest.mark.parametrize("environment", ["development", "production"])
def test_startup_rejects_normalized_example(example_key: str, environment: str) -> None:
    # Given an example wrapped as an env-file implementation might preserve it.
    # When / Then startup rejects it in either environment.
    with pytest.raises(ProductionConfigError, match="publicly known example"):
        validate_admin_api_key(environment=environment, admin_key=example_key)


@pytest.mark.parametrize("surface", ["middleware", "router", "health"])
def test_runtime_disables_normalized_example(
    monkeypatch: pytest.MonkeyPatch, example_key: str, surface: Literal["middleware", "router", "health"],
) -> None:
    # Given startup is bypassed and each consumer is exercised independently.
    monkeypatch.setenv("ADMIN_API_KEY", example_key)
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setattr(
        "shorts_api.routes.admin.admin_service.get_system_health",
        AsyncMock(return_value={"status": "ok", "db": {}, "redis": {}, "uptime_seconds": 1}),
    )
    isolated = FastAPI()
    path = "/api/admin/health"
    expected_status = 403
    match surface:
        case "middleware":
            isolated.add_middleware(ApiKeyMiddleware)
            expected_status = 401
        case "router":
            isolated.include_router(router, prefix="/api/admin")
        case "health":
            register_health_routes(isolated)
            monkeypatch.setattr("shorts_api.main.model_health.check_all", AsyncMock(return_value=[]))
            path = "/health"
            expected_status = 200
        case unreachable:
            assert_never(unreachable)
    # When a caller supplies the exact configured value (ASGI permits CR/LF fixtures).
    with TestClient(isolated) as client:
        response = client.get(path, headers={"X-Admin-Key": example_key})
    # Then the credential is disabled, including public health detail redaction.
    assert configured_admin_api_key() == ""
    assert response.status_code == expected_status
    if surface == "health":
        assert response.json() == {"status": "degraded"}


@pytest.mark.parametrize("key", [
    "synthetic-local-admin-key-change-me-unique",
    ' \t"synthetic-local-admin-key-change-me-unique" ',
    "'local-admin-key-change-me\"",  # Unmatched quotes are not one matching layer.
    '"\'local-admin-key-change-me\'"',  # Strip exactly one quote layer.
])
def test_genuine_key_is_preserved_and_authenticates(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    # Given a genuine key, including significant case, quotes and whitespace.
    monkeypatch.setenv("ADMIN_API_KEY", key)
    monkeypatch.setenv("ENVIRONMENT", "production")
    isolated = FastAPI()
    isolated.add_middleware(ApiKeyMiddleware)
    isolated.include_router(router, prefix="/api/admin")
    monkeypatch.setattr(
        "shorts_api.routes.admin.admin_service.get_system_health",
        AsyncMock(return_value={"status": "ok", "db": {}, "redis": {}, "uptime_seconds": 1}),
    )
    validate_admin_api_key()
    # When authenticating through both runtime gates.
    with TestClient(isolated) as client:
        response = client.get("/api/admin/health", headers={"X-Admin-Key": key})
    # Then exact input bytes remain the credential, not their normalized representation.
    assert configured_admin_api_key() == key
    assert response.status_code == 200


@pytest.mark.parametrize("environment", ["development", "staging", "custom"])
def test_empty_key_remains_allowed_outside_production(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    # Given disabled admin access in a non-production environment.
    monkeypatch.setenv("ADMIN_API_KEY", "")
    # When / Then startup allows it and runtime has no credential.
    validate_admin_api_key(environment=environment)
    assert configured_admin_api_key() == ""
