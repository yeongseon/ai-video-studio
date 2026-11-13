from __future__ import annotations

import hashlib
import importlib
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest


def _object_storage_module():
    return importlib.import_module("creator_service.object_storage")


def test_upload_bytes_returns_storage_result(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    payload = b"artifact-bytes"

    result = backend.upload("run-1/audio.wav", payload, content_type="audio/wav")

    assert result.key == "run-1/audio.wav"
    assert result.size_bytes == len(payload)
    assert result.content_type == "audio/wav"
    assert result.checksum == hashlib.md5(payload).hexdigest()
    assert result.storage_provider == "local"


def test_upload_binary_io_returns_storage_result(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    payload = b"streamed-artifact"

    result = backend.upload(
        "run-2/script.json",
        BytesIO(payload),
        content_type="application/json",
    )

    assert result.key == "run-2/script.json"
    assert result.size_bytes == len(payload)
    assert result.content_type == "application/json"
    assert result.checksum == hashlib.md5(payload).hexdigest()
    assert result.storage_provider == "local"


def test_download_url_returns_artifact_api_path(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    key = "run-3/image.png"

    url = backend.download_url(key)

    assert url == f"/api/artifacts/files/{key}"


def test_download_bytes_returns_uploaded_data(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    key = "run-4/subtitles.srt"
    payload = b"1\n00:00:00,000 --> 00:00:01,000\nhello\n"
    backend.upload(key, payload)

    downloaded = backend.download_bytes(key)

    assert downloaded == payload


def test_delete_removes_existing_file(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    key = "run-5/render.mp4"
    backend.upload(key, b"video-bytes")

    deleted = backend.delete(key)

    assert deleted is True
    assert backend.exists(key) is False


def test_delete_returns_false_for_missing_file(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))

    deleted = backend.delete("missing/file.bin")

    assert deleted is False


def test_local_delete_normalizes_non_missing_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a local object whose filesystem deletion fails.
    module = _object_storage_module()
    backend = module.LocalStorageBackend(root=str(tmp_path))
    key = "run-5/render.mp4"
    backend.upload(key, b"video-bytes")

    def fail_unlink(_path: str, *, dir_fd: int | None = None) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "unlink", fail_unlink)

    # When deletion crosses the storage adapter boundary.
    with pytest.raises(module.StorageDeletionError) as failure:
        backend.delete(key)

    # Then callers receive the shared typed deletion failure.
    assert failure.value.key == key


def test_exists_returns_true_and_false(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    key = "run-6/asset.txt"

    assert backend.exists(key) is False
    backend.upload(key, b"content")
    assert backend.exists(key) is True


def test_factory_returns_local_backend_by_default(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    module = _object_storage_module()
    backend = module.create_storage_backend()

    assert isinstance(backend, module.LocalStorageBackend)


def test_path_traversal_upload_rejected(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    import pytest

    with pytest.raises(ValueError, match="path traversal"):
        backend.upload("../../etc/passwd", b"evil")


def test_path_traversal_download_rejected(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    import pytest

    with pytest.raises(ValueError, match="path traversal"):
        backend.download_bytes("../../../etc/shadow")


def test_path_traversal_delete_rejected(tmp_path):
    backend = _object_storage_module().LocalStorageBackend(root=str(tmp_path))
    import pytest

    with pytest.raises(ValueError, match="path traversal"):
        backend.delete("../../etc/passwd")


def test_lazy_singleton_does_not_crash_at_import(monkeypatch):
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    # Tests that already resolved the singleton must not affect the import-time contract.
    # Import in a fresh interpreter, then verify the first backend access there.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import creator_service.object_storage as module\n"
                "assert module._storage_backend is None\n"
                "backend = module.get_storage_backend()\n"
                "assert isinstance(backend, module.LocalStorageBackend)\n"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
