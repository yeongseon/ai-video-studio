from threading import Event

import anyio
import httpx
import pytest
from starlette.requests import Request

from tests.project_preview_support import PreviewFixture, preview as preview
from tests.stream_http_support import http_stream


@pytest.mark.parametrize("length_delta", [-1, 1])
@pytest.mark.asyncio
async def test_first_block_length_mismatch_returns_generic_502(
    client: httpx.AsyncClient, preview: PreviewFixture, monkeypatch: pytest.MonkeyPatch, length_delta: int,
) -> None:
    # Given an owned object and an adapter returning fewer/more bytes than requested.
    from tests.project_preview_support import content_url

    asset = await preview.asset()
    monkeypatch.setattr(preview.backend, "read_range", lambda key, start, count: b"media-marker" * 10 if length_delta > 0 else b"short-media")
    # When the first read violates the contract before response headers.
    response = await client.get(content_url(asset))
    # Then the clean error response contains no media bytes or diagnostic text.
    assert response.status_code == 502
    assert response.json() == {"detail": "Media storage unavailable"}
    assert b"media-marker" not in response.content
    assert b"short-media" not in response.content


def test_later_short_read_aborts_real_http_without_media_error_body(tmp_path, monkeypatch, caplog) -> None:
    # Given a three-block object whose second read is short but nonempty.
    from tests.project_preview_support import CountedStorage

    backend = CountedStorage(tmp_path)
    backend.size = 3 * 65536
    calls = []

    def read(key, offset, length):
        calls.append(offset)
        return b"a" * (length - 1 if offset == 65536 else length)

    monkeypatch.setattr(backend, "read_range", read)
    chunks = []
    # When actual HTTP consumes the stream (not an in-process function result).
    with http_stream(backend, "private-key-marker") as client:
        with pytest.raises(httpx.RemoteProtocolError):
            with client.stream("GET", "/content") as response:
                assert response.status_code == 200
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
    # Then only the first block was transmitted and the transfer is incomplete.
    assert b"".join(chunks) == b"a" * 65536
    assert calls == [0, 65536]
    assert "private-key-marker" not in caplog.text


def test_midstream_storage_error_cannot_complete_real_http(tmp_path, monkeypatch, caplog) -> None:
    # Given a backend that fails after one successful chunk.
    from tests.project_preview_support import CountedStorage
    from creator_service.object_storage import StorageReadError

    backend = CountedStorage(tmp_path)
    backend.size = 131072

    def read(key, offset, length):
        if offset:
            raise StorageReadError("generation_changed")
        return b"a" * length

    monkeypatch.setattr(backend, "read_range", read)
    chunks = []
    # When a storage exception arrives after headers through real HTTP.
    with http_stream(backend, "private-marker") as client:
        with pytest.raises(httpx.RemoteProtocolError):
            with client.stream("GET", "/content") as response:
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
    # Then even swallowing the exception cannot turn this into a complete transfer.
    assert b"".join(chunks) == b"a" * 65536
    assert "private-marker" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_during_inflight_read_waits_for_release_then_closes(preview, monkeypatch) -> None:
    # Given a read held in a worker thread while response cancellation arrives.
    from shorts_api.media_streaming import stream_media

    entered, release, finished = Event(), Event(), anyio.Event()
    original = preview.backend.read_range

    def held(key, offset, count):
        preview.backend.active += 1
        entered.set()
        try:
            assert release.wait(5)
            return original(key, offset, count)
        finally:
            preview.backend.active -= 1

    monkeypatch.setattr(preview.backend, "read_range", held)

    async def consume():
        try:
            await stream_media(preview.backend, "private-key", Request({"type": "http", "method": "GET", "headers": []}), "video/mp4")
        finally:
            finished.set()

    # When cancellation interrupts an actually in-flight backend operation.
    async with anyio.create_task_group() as group:
        group.start_soon(consume)
        assert await anyio.to_thread.run_sync(entered.wait, 2)
        group.cancel_scope.cancel()
        assert preview.backend.active == 1
        release.set()
    # Then the synchronous operation releases its resources before cancellation returns.
    assert finished.is_set()
    assert preview.backend.active == 0
    assert len(preview.backend.reads) == 1
