import errno
import os

import httpx
import pytest

from creator_service.storage_read_session import LocalReader
from tests.stream_http_support import http_stream
from tests.test_local_descriptor_lifetime import RetainedBackend, assert_closed


@pytest.mark.parametrize("phase", ["prefetch", "head", "empty", "midstream"])
@pytest.mark.parametrize("error_type", [OSError, PermissionError, TimeoutError])
def test_local_syscall_failure_is_private_and_closes_reader_over_http(tmp_path, monkeypatch, caplog, phase, error_type):
    # Given an operational syscall failure with private filesystem diagnostics.
    marker = "private-filesystem-marker-943"
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"a" * (0 if phase == "empty" else 131072))
    original_read, original_verify = LocalReader.read_range, LocalReader.verify

    def fail(*args):
        raise error_type(errno.EIO, marker)

    def read(reader, offset, length):
        if phase == "prefetch" or (phase == "midstream" and offset):
            with monkeypatch.context() as patch:
                patch.setattr(os, "pread", fail)
                return original_read(reader, offset, length)
        return original_read(reader, offset, length)

    def verify(reader):
        if phase in ("head", "empty"):
            with monkeypatch.context() as patch:
                patch.setattr(os, "fstat", fail)
                return original_verify(reader)
        return original_verify(reader)

    monkeypatch.setattr(LocalReader, "read_range", read)
    monkeypatch.setattr(LocalReader, "verify", verify)
    # When real ASGI/HTTP delivery encounters the failed read or metadata operation.
    with http_stream(backend, "object") as client:
        if phase == "midstream":
            with pytest.raises(httpx.RemoteProtocolError):
                client.get("/content")
        else:
            response = client.request("HEAD" if phase == "head" else "GET", "/content")
            assert response.status_code == (503 if error_type is TimeoutError else 502)
            assert marker not in response.text
    # Then normalized diagnostics never reach responses/logs and retained FDs are closed.
    assert marker not in caplog.text
    assert_closed(backend.readers[0])
