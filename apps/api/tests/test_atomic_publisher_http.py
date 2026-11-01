import os

import pytest

from creator_service.object_storage import LocalStorageBackend
from tests.stream_http_support import http_stream


@pytest.mark.parametrize("alias", [False, True])
def test_concurrent_upload_preserves_generation_over_real_http(tmp_path, monkeypatch, alias) -> None:
    # Given a published A inode and a hard link that an in-place writer would mutate.
    backend = LocalStorageBackend(str(tmp_path))
    target = tmp_path / "object"
    target.write_bytes(b"a" * 131072)
    link = tmp_path / "alias"
    os.link(target, link)
    original_stat = target.stat()
    pread = os.pread
    overwritten = False

    def upload_between_chunks(fd, size, offset):
        nonlocal overwritten
        if offset and not overwritten:
            overwritten = True
            backend.upload("object", b"b" * 131072)
            if not alias:
                os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return pread(fd, size, offset)

    monkeypatch.setattr(os, "pread", upload_between_chunks)
    # When upload overwrites a key while HTTP is delivering the old generation.
    with http_stream(backend, "object") as client:
        response = client.get("/content")
    # Then neither timestamp restoration nor hard links let publication mix A and B.
    assert response.status_code == 200
    assert response.content == b"a" * 131072
    assert link.read_bytes() == b"a" * 131072
    assert target.read_bytes() == b"b" * 131072
    assert link.stat().st_mtime_ns == original_stat.st_mtime_ns
