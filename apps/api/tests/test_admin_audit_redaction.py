"""Production audit records retain attribution at the JSON logging boundary."""

import hashlib
import json
import logging
from unittest.mock import AsyncMock

import pytest
from creator_service.error_redaction import is_sensitive_log_key
from creator_service.logging_config import JsonFormatter
from starlette.requests import Request

from shorts_api.routes import admin


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["unstick", "clear", "clear-pattern", "dangerous", "prefix"])
async def test_audit_fingerprint_survives_when_record_contains_a_secret(
    operation: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real admin mutation path with synthetic credentials and request metadata.
    key = "synthetic-admin-credential"
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:8]
    assert not is_sensitive_log_key("key_fingerprint")
    request = Request({"type": "http", "headers": [(b"x-request-id", b"token=synthetic-secret")],
                       "client": ("127.0.0.1", 1234)})
    monkeypatch.setattr(admin.admin_service, "unstick_run", AsyncMock(return_value={}))
    monkeypatch.setattr(admin.admin_service, "clear_cache", AsyncMock(return_value={}))
    caplog.set_level(logging.WARNING, logger="admin.audit")
    # When the production code emits the audit record, then the production formatter handles it.
    if operation == "unstick":
        await admin.admin_unstick_run("42", request, key, None)
    else:
        pattern = {"clear": None, "clear-pattern": "cache:test:*", "dangerous": "*", "prefix": "other:*"}[operation]
        if operation in {"dangerous", "prefix"}:
            with pytest.raises(admin.HTTPException):
                await admin.admin_clear_cache(request, pattern, False, key, None)
        else:
            await admin.admin_clear_cache(request, pattern, True, key, None)
    records = [record for record in caplog.records if record.name == "admin.audit"]
    assert records
    for record in records:
        raw = JsonFormatter("api").format(record)
        assert f"key_fingerprint={fingerprint}" in json.loads(raw)["message"]
        assert key not in raw and "synthetic-secret" not in raw
