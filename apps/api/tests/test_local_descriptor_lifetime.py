import errno
import os
from pathlib import Path
from threading import Event

import anyio
import httpx
import pytest
from starlette.requests import Request

from creator_service.object_storage import LocalStorageBackend
from creator_service.object_storage_reads import StorageReadError
from creator_service.storage_read_session import LocalReader
from shorts_api.media_streaming import stream_media
from tests.stream_http_support import http_stream


class RetainedBackend(LocalStorageBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.readers: list[LocalReader] = []

    def open_read(self, key: str):
        reader = super().open_read(key)
        self.readers.append(reader)
        return reader


def assert_closed(reader: LocalReader) -> None:
    with pytest.raises(OSError) as error:
        os.fstat(reader._descriptor)
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("method,size", [("GET", 131072), ("HEAD", 10), ("GET", 0)])
def test_local_reader_fd_is_closed_immediately_after_http_completion(tmp_path, method, size) -> None:
    # Given retained reader references (no garbage collection can mask leaks).
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"a" * size)
    # When normal/HEAD/empty HTTP delivery finishes.
    with http_stream(backend, "object") as client:
        response = client.request(method, "/content")
        assert response.status_code == 200
    # Then resource ownership was released despite the retained response/readers.
    assert len(backend.readers) == 1
    assert_closed(backend.readers[0])


def test_local_reader_fd_is_closed_after_midstream_failure(tmp_path, monkeypatch) -> None:
    # Given a retained reader that fails on its second block.
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"a" * 131072)
    original = LocalReader.read_range

    def fail(reader, offset, count):
        if offset:
            raise StorageReadError()
        return original(reader, offset, count)

    monkeypatch.setattr(LocalReader, "read_range", fail)
    # When real HTTP observes an incomplete transfer.
    with http_stream(backend, "object") as client:
        with pytest.raises(httpx.RemoteProtocolError):
            client.get("/content")
    # Then retained references do not defer descriptor close to GC.
    assert_closed(backend.readers[0])


@pytest.mark.asyncio
async def test_local_inflight_disconnect_closes_retained_fd(tmp_path, monkeypatch) -> None:
    # Given a local second read actually held in the bounded worker thread.
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"a" * 131072)
    entered, release = Event(), Event()
    original = LocalReader.read_range

    def held(reader, offset, count):
        if offset:
            entered.set()
            assert release.wait(5)
        return original(reader, offset, count)

    monkeypatch.setattr(LocalReader, "read_range", held)
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.3"}}
    response = await stream_media(backend, "object", Request(scope), "video/mp4")

    async def send(message):
        return None

    async def receive():
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        release.set()
        return {"type": "http.disconnect"}

    # When disconnect arrives during that read, retain response + session.
    with anyio.fail_after(5):
        await response(scope, receive, send)
    # Then close is synchronous with response ownership ending.
    assert_closed(backend.readers[0])


def test_local_reader_constructor_failure_closes_open_descriptor(tmp_path, monkeypatch) -> None:
    # Given a constructor failure after acquisition, retaining the owner itself.
    from creator_service.object_storage_reads import local_descriptor

    (tmp_path / "object").write_bytes(b"data")
    owner = local_descriptor(tmp_path, "object")
    original = os.fstat
    descriptors = []

    def fail(fd):
        descriptors.append(fd)
        raise OSError("metadata unavailable")

    monkeypatch.setattr(os, "fstat", fail)
    # The descriptor opener also stats: fail only on the constructor's second stat.
    calls = 0

    def second(fd):
        nonlocal calls
        calls += 1
        return original(fd) if calls == 1 else fail(fd)

    monkeypatch.setattr(os, "fstat", second)
    # When constructor metadata acquisition fails.
    with pytest.raises(OSError):
        LocalReader(owner)
    # Then even a retained context manager owns no open descriptor.
    monkeypatch.setattr(os, "fstat", original)
    with pytest.raises(OSError) as error:
        original(descriptors[0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("method,size", [("HEAD", 4), ("GET", 0)])
def test_head_empty_verify_failure_is_generic_and_closes_fd(tmp_path, monkeypatch, method, size) -> None:
    # Given mutation of a real local file while its no-body response verifies.
    backend = RetainedBackend(tmp_path)
    target = tmp_path / "object"
    target.write_bytes(b"a" * size)
    original = LocalReader.verify

    def changed(reader):
        with target.open("ab") as file:
            file.write(b"new")
        original(reader)

    monkeypatch.setattr(LocalReader, "verify", changed)
    # When HEAD/empty GET detects that changed descriptor.
    with http_stream(backend, "object") as client:
        response = client.request(method, "/content")
    # Then storage error normalization applies to this await too.
    assert response.status_code == 502
    if method == "GET":
        assert response.json() == {"detail": "Media storage unavailable"}
    assert_closed(backend.readers[0])
