"""Tests for admin API audit enrichment and audit_id tracking."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from shorts_api.main import app
from shorts_api.routes.admin import require_admin, require_confirmation_and_rate_limit


@pytest.fixture
def client():
    """Fixture for FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def mock_admin_service():
    """Mock admin_service."""
    with patch("shorts_api.routes.admin.admin_service") as mock:
        yield mock


@pytest.fixture
def admin_overrides(mock_admin_service, monkeypatch):
    """Setup dependency overrides for admin endpoints."""
    # Middleware-level auth requires ADMIN_API_KEY to match the header
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")

    async def _require_admin(x_admin_key: str | None = None) -> str:
        return x_admin_key or "test-admin-key"

    async def _require_confirmation(
        x_confirm_action: str | None = None, x_admin_key: str = "test-key"
    ):
        return None

    app.dependency_overrides[require_admin] = _require_admin
    app.dependency_overrides[require_confirmation_and_rate_limit] = _require_confirmation
    yield
    app.dependency_overrides.pop(require_admin, None)
    app.dependency_overrides.pop(require_confirmation_and_rate_limit, None)

def test_admin_unstick_run_includes_audit_id(client, mock_admin_service, admin_overrides, caplog):
    """Test that unstick_run response includes audit_id."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.unstick_run = AsyncMock(
        return_value={"ok": True, "run_id": "42", "current_stage": "SCRIPT_REVIEW"}
    )

    response = client.post(
        "/api/admin/runs/42/unstick",
        headers={"X-Admin-Key": "test-admin-key", "X-Confirm-Action": "yes"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["audit_id"] is not None
    assert len(data["audit_id"]) == 36  # UUID length
    assert "audit_id=" in caplog.text


def test_admin_clear_cache_includes_audit_id(client, mock_admin_service, admin_overrides, caplog):
    """Test that clear_cache response includes audit_id."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.clear_cache = AsyncMock(
        return_value={
            "ok": True,
            "deleted_keys": 5,
            "key_pattern": "cache:test:*",
            "dry_run": False,
            "matched_keys": ["cache:test:1", "cache:test:2"],
        }
    )

    response = client.post(
        "/api/admin/cache/clear",
        params={"key_pattern": "cache:test:*", "dry_run": "false"},
        headers={"X-Admin-Key": "test-admin-key", "X-Confirm-Action": "yes"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["audit_id"] is not None
    assert len(data["audit_id"]) == 36  # UUID length
    assert "audit_id=" in caplog.text


def test_audit_log_includes_request_id(client, mock_admin_service, admin_overrides, caplog):
    """Test that audit logs include X-Request-Id from headers."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.unstick_run = AsyncMock(
        return_value={"ok": True, "run_id": "99", "current_stage": "SCRIPT_REVIEW"}
    )

    response = client.post(
        "/api/admin/runs/99/unstick",
        headers={
            "X-Admin-Key": "test-admin-key",
            "X-Confirm-Action": "yes",
            "X-Request-Id": "req-12345",
        },
    )

    assert response.status_code == 200
    assert "request_id=req-12345" in caplog.text


def test_audit_log_includes_source_ip(client, mock_admin_service, admin_overrides, caplog):
    """Test that audit logs include source_ip from request client."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.unstick_run = AsyncMock(
        return_value={"ok": True, "run_id": "100", "current_stage": "SCRIPT_REVIEW"}
    )

    response = client.post(
        "/api/admin/runs/100/unstick",
        headers={"X-Admin-Key": "test-admin-key", "X-Confirm-Action": "yes"},
    )

    assert response.status_code == 200
    # Note: TestClient automatically includes client info
    assert "source_ip=" in caplog.text


def test_audit_log_has_correct_format(client, mock_admin_service, admin_overrides, caplog):
    """Test that audit logs follow the expected format with all enriched fields."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.unstick_run = AsyncMock(
        return_value={"ok": True, "run_id": "101", "current_stage": "SCRIPT_REVIEW"}
    )

    response = client.post(
        "/api/admin/runs/101/unstick",
        headers={"X-Admin-Key": "test-admin-key", "X-Confirm-Action": "yes"},
    )

    assert response.status_code == 200
    log_output = caplog.text
    # Verify all expected fields are in the audit log
    assert "ADMIN_ACTION: unstick_run" in log_output
    assert "run_id=" in log_output
    assert "key_fingerprint=" in log_output  # key fingerprint
    assert "request_id=" in log_output
    assert "source_ip=" in log_output
    assert "audit_id=" in log_output


def test_audit_log_cache_clear_has_correct_format(
    client, mock_admin_service, admin_overrides, caplog
):
    """Test that cache_clear audit logs follow the expected enriched format."""
    caplog.set_level(logging.WARNING, logger="admin.audit")

    mock_admin_service.clear_cache = AsyncMock(
        return_value={
            "ok": True,
            "deleted_keys": 3,
            "key_pattern": "session:*",
            "dry_run": False,
        }
    )

    response = client.post(
        "/api/admin/cache/clear",
        params={"key_pattern": "session:*", "dry_run": "false"},
        headers={
            "X-Admin-Key": "test-admin-key",
            "X-Confirm-Action": "yes",
            "X-Request-Id": "req-999",
        },
    )

    assert response.status_code == 200
    log_output = caplog.text
    # Verify all expected fields for cache_clear
    assert "ADMIN_ACTION: cache_clear" in log_output
    assert "key_pattern=session:*" in log_output
    assert "key_fingerprint=" in log_output  # key fingerprint
    assert "request_id=req-999" in log_output
    assert "source_ip=" in log_output
    assert "audit_id=" in log_output
