import os
from pathlib import Path

import httpx
import pytest
from creator_service.object_storage import LocalStorageBackend

from tests.stream_http_support import http_stream


@pytest.mark.parametrize("change", ["replace", "grow", "shrink"])
@pytest.mark.parametrize("offset", [0, 65536])
@pytest.mark.parametrize("range_header", [None, "bytes=1-131070"])
def test_local_stream_never_mixes_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, offset: int, range_header: str | None,
) -> None:
    # Given generation A and a replacement/mutation synchronized to a particular read.
    target = tmp_path / "object"
    original = b"a" * 131072
    target.write_bytes(original)
    backend = LocalStorageBackend(str(tmp_path))
    pread = os.pread
    changed = False

    def mutate(descriptor, count, position):
        nonlocal changed
        if not changed and position >= offset:
            changed = True
            if change == "replace":
                replacement = tmp_path / "replacement"
                replacement.write_bytes(b"b" * len(original))
                os.replace(replacement, target)
            elif change == "grow":
                with target.open("ab") as file:
                    file.write(b"growth")
            else:
                with target.open("r+b") as file:
                    file.truncate(65537)
        return pread(descriptor, count, position)

    monkeypatch.setattr(os, "pread", mutate)
    # When a full/ranged request crosses replacement or mutation over real HTTP.
    with http_stream(backend, "object") as client:
        headers = {"Range": range_header} if range_header else {}
        if change == "replace":
            response = client.get("/content", headers=headers)
            # Then retained generation A, never mixed A/B, is delivered.
            assert response.status_code == (206 if range_header else 200)
            assert response.content == (original[1:131071] if range_header else original)
        elif offset == 0:
            response = client.get("/content", headers=headers)
            assert response.status_code == 502
            assert response.json() == {"detail": "Media storage unavailable"}
        else:
            with pytest.raises(httpx.RemoteProtocolError):
                client.get("/content", headers=headers)


def test_local_open_read_metadata_retains_generation_before_first_read(tmp_path: Path) -> None:
    # Given a descriptor acquired with metadata for A before an atomic rename.
    backend = LocalStorageBackend(str(tmp_path))
    target = tmp_path / "object"
    target.write_bytes(b"aaaa")
    reader = backend.open_read("object")
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"bbbbbb")
    os.replace(replacement, target)
    try:
        # When reading the retained generation after replacement.
        assert reader.info.size == 4
        assert reader.read_range(0, 4) == b"aaaa"
        reader.verify()
    finally:
        reader.close()
