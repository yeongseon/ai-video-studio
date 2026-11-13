"""Pytest fixtures for Celery app tests."""

from unittest.mock import AsyncMock

import pytest
from celery_app import celery_app
from creator_service.object_storage import StorageResult
from creator_service.rejected_scene_cleanup import rejected_scene_cleanup_service


@pytest.fixture
def celery_config():
    """Celery configuration for testing."""
    return {
        "broker_url": "memory://",
        "result_backend": "cache+memory://",
    }


@pytest.fixture
def app(celery_config):
    """Celery app fixture with test configuration."""
    celery_app.conf.update(celery_config)
    return celery_app


@pytest.fixture(autouse=True)
def stub_artifact_upload(monkeypatch: pytest.MonkeyPatch):
    def _store_artifact_file(run_id: int, local_path: str, content_type: str):
        return StorageResult(
            key=f"{run_id}/{local_path}",
            size_bytes=0,
            content_type=content_type,
            checksum="test-checksum",
            storage_provider="local",
        )

    monkeypatch.setattr(
        "creator_service.artifact_storage_integration.store_artifact_file",
        _store_artifact_file,
    )
    monkeypatch.setattr(rejected_scene_cleanup_service, "record_intent", AsyncMock(return_value=1))
