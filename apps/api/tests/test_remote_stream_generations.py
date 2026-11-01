import httpx
import pytest

from creator_service.object_storage_reads import ObjectInfo, StorageReadError
from creator_service.storage_read_session import RemoteReader
from tests.stream_http_support import http_stream


class ConditionalStorage:
    def __init__(self, change: str, before_first: bool) -> None:
        self.data = b"a" * 131072
        self.etag = "a"
        self.change = change
        self.before_first = before_first
        self.calls = 0

    def open_read(self, key):
        info = ObjectInfo(len(self.data), self.etag)

        def read(offset, count):
            if self.calls == (0 if self.before_first else 1):
                self.etag = "b"
                self.data = (b"b" * 131072 if self.change == "replace" else
                             self.data + b"growth" if self.change == "grow" else self.data[:65537])
            self.calls += 1
            if self.etag != info.etag:
                raise StorageReadError("generation_changed")
            return self.data[offset:offset+count]

        return RemoteReader(info, read)


@pytest.mark.parametrize("change", ["replace", "grow", "shrink"])
@pytest.mark.parametrize("before_first", [True, False])
@pytest.mark.parametrize("range_header", [None, "bytes=1-131070"])
def test_remote_generation_change_never_completes_http(tmp_path, change, before_first, range_header) -> None:
    # Given ETag conditional semantics with same-size replacement, growth or shrink.
    backend = ConditionalStorage(change, before_first)
    # When a full/ranged real HTTP response spans the generation change.
    with http_stream(backend, "object") as client:
        headers = {"Range": range_header} if range_header else {}
        if before_first:
            response = client.get("/content", headers=headers)
            assert response.status_code == 502
            assert response.json() == {"detail": "Media storage unavailable"}
        else:
            chunks = []
            with pytest.raises(httpx.RemoteProtocolError):
                with client.stream("GET", "/content", headers=headers) as response:
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
            # Then no new-generation bytes or embedded JSON reach the client.
            assert b"".join(chunks) == b"a" * 65536


def test_head_uses_one_generation_metadata_without_read(tmp_path) -> None:
    # Given generation A and a backend which would replace it on the first read.
    backend = ConditionalStorage("replace", True)
    # When HEAD is requested over real HTTP.
    with http_stream(backend, "object") as client:
        response = client.head("/content")
    # Then metadata describes A and no read occurred.
    assert response.status_code == 200 and response.headers["content-length"] == "131072"
    assert backend.calls == 0
