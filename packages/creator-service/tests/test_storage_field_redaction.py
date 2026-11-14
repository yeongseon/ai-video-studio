"""Storage diagnostics use a non-credential field name at the real JSON boundary."""

import json
import logging
from types import SimpleNamespace

import pytest

from creator_service.error_redaction import is_sensitive_log_key
from creator_service.logging_config import JsonFormatter
from creator_service.object_storage import AzureBlobStorageBackend


def test_object_name_survives_when_unsigned_storage_warning_contains_a_secret(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the actual Azure fallback without constructing an optional cloud SDK client.
    backend = AzureBlobStorageBackend.__new__(AzureBlobStorageBackend)
    monkeypatch.setattr(backend, "_prefix", "", raising=False)
    monkeypatch.setattr(backend, "_client", SimpleNamespace(credential=None), raising=False)
    monkeypatch.setattr(backend, "_container_client", SimpleNamespace(
        get_blob_client=lambda _: SimpleNamespace(url="https://storage.invalid/blob"),
    ), raising=False)
    monkeypatch.setenv("STORAGE_ALLOW_UNSIGNED_URLS", "true")
    assert not is_sensitive_log_key("object_name")
    caplog.set_level(logging.WARNING, logger="creator_service.object_storage")
    # When the real storage method logs its object name and an embedded sensitive assignment.
    backend.download_url("scene-42;token=synthetic-secret")
    record = next(record for record in caplog.records if record.name == "creator_service.object_storage")
    raw = JsonFormatter("api").format(record)
    # Then the object identifier remains useful but the genuine secret is hidden.
    assert "object_name=scene-42;token=<redacted>" in json.loads(raw)["message"]
    assert "synthetic-secret" not in raw
