"""Exercise each admin comparison independently through raw HTTP headers."""

import logging
from collections.abc import Iterator
from typing import assert_never
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from shorts_api.auth import ApiKeyMiddleware
from shorts_api.health import register_health_routes
from shorts_api.routes.admin import require_admin


@pytest.fixture(params=["middleware", "router", "health"])
def admin_surface(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, str]]:
    monkeypatch.setenv("ADMIN_API_KEY", "synthetic-admin-key-1057")
    monkeypatch.setenv("ENVIRONMENT", "development")
    isolated = FastAPI()
    path = "/api/admin/probe"
    dependencies = []
    match request.param:
        case "middleware":
            isolated.add_middleware(ApiKeyMiddleware)
        case "router":
            dependencies = [Depends(require_admin)]
        case "health":
            register_health_routes(isolated)
            monkeypatch.setattr("shorts_api.main.model_health.check_all", AsyncMock(return_value=[]))
            path = "/health"
        case unreachable:
            assert_never(unreachable)

    @isolated.get("/api/admin/probe", dependencies=dependencies)
    def probe() -> dict[str, str]:
        return {"status": "authenticated"}

    with TestClient(isolated, raise_server_exceptions=False) as client:
        yield client, path


@pytest.mark.parametrize("header", [b"\xff", b"\xc3\xa9"])
def test_wrong_key_response_when_header_is_non_ascii(
    admin_surface: tuple[TestClient, str], header: bytes, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given the wrong-key response on this independent gate.
    client, path = admin_surface
    wrong = client.get(path, headers={"X-Admin-Key": "wrong-ascii-key"})
    caplog.clear()
    # When raw bytes become Latin-1 text at the ASGI boundary.
    response = client.get(path, headers=[(b"X-Admin-Key", header)])
    # Then neither the response nor logging distinguishes it from a wrong key.
    assert response.status_code == wrong.status_code
    assert response.content == wrong.content
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not [record for record in caplog.records if record.exc_info]


def test_authenticates_when_header_is_correct(admin_surface: tuple[TestClient, str]) -> None:
    # Given an ASCII credential accepted by each independent gate.
    client, path = admin_surface
    # When the exact key is supplied.
    response = client.get(path, headers={"X-Admin-Key": "synthetic-admin-key-1057"})
    # Then the protected view is available (including health detail).
    assert response.status_code == 200
    expected = {"status": "degraded", "models": {}} if path == "/health" else {
        "status": "authenticated",
    }
    assert response.json() == expected


def test_wrong_key_response_when_utf8_header_differs_from_configured_decoded_text(
    admin_surface: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a non-ASCII configured key, compared as decoded text, not wire bytes.
    client, path = admin_surface
    wrong = client.get(path, headers={"X-Admin-Key": "wrong-ascii-key"})
    monkeypatch.setenv("ADMIN_API_KEY", "é")
    caplog.clear()
    # When UTF-8 bytes decode as Latin-1 "Ã©", not the configured "é".
    response = client.get(path, headers=[(b"X-Admin-Key", b"\xc3\xa9")])
    # Then there is no extra decoding, authentication, server error or traceback.
    assert response.status_code == wrong.status_code
    assert response.content == wrong.content
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not [record for record in caplog.records if record.exc_info]
