import errno
import os

import pytest

from creator_service.object_storage import LocalStorageBackend
from creator_service.object_storage_reads import local_descriptor
from creator_service.storage_read_session import LocalReader
from tests.stream_http_support import http_stream


@pytest.mark.parametrize("syscall,error,status", [("open", OSError(errno.EIO, "private-acquire-marker"), 502),
                                                ("fstat", OSError(errno.ESTALE, "private-acquire-marker"), 502),
                                                ("open", TimeoutError("private-acquire-marker"), 503)])
def test_operational_acquisition_failure_is_unavailable_not_missing(tmp_path, monkeypatch, caplog, syscall, error, status):
    backend = LocalStorageBackend(str(tmp_path))
    (tmp_path / "object").write_bytes(b"data")
    original = getattr(os, syscall)

    def failed(*args, **kwargs):
        if syscall == "fstat" or args[0] == "object":
            raise error
        return original(*args, **kwargs)

    with http_stream(backend, "object") as client:
        baseline = len(os.listdir("/proc/self/fd"))
        monkeypatch.setattr(os, syscall, failed)
        response = client.get("/content")
        monkeypatch.setattr(os, syscall, original)
        assert response.status_code == status
        assert len(os.listdir("/proc/self/fd")) == baseline
        assert "private-acquire-marker" not in response.text
    assert "private-acquire-marker" not in caplog.text


@pytest.mark.parametrize("key", ["missing", "symlink"])
def test_missing_and_symlink_acquisition_still_returns_404(tmp_path, key):
    (tmp_path / "object").write_bytes(b"data")
    (tmp_path / "symlink").symlink_to(tmp_path / "object")
    with http_stream(LocalStorageBackend(str(tmp_path)), key) as client:
        assert client.get("/content").status_code == 404


@pytest.mark.parametrize("error_type", [MemoryError, ValueError])
def test_constructor_non_oserror_closes_retained_owner(tmp_path, monkeypatch, error_type):
    (tmp_path / "object").write_bytes(b"data")
    owner = local_descriptor(tmp_path, "object")
    original = os.fstat
    descriptors = []

    def failed(fd):
        descriptors.append(fd)
        if len(descriptors) == 2:
            raise error_type("private-constructor-marker")
        return original(fd)

    monkeypatch.setattr(os, "fstat", failed)
    with pytest.raises(error_type):
        LocalReader(owner)
    with pytest.raises(OSError) as error:
        original(descriptors[-1])
    assert error.value.errno == errno.EBADF
