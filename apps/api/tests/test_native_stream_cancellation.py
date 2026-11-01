import asyncio
import os
from threading import Event, Lock

import anyio
import pytest
from starlette.requests import Request

from creator_service.storage_read_session import LocalReader
from shorts_api import media_streaming
from tests.test_local_descriptor_lifetime import RetainedBackend, assert_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["acquire", "prefetch", "midstream", "head", "close"])
async def test_raw_cancellation_waits_for_worker_before_closing_retained_readers(tmp_path, monkeypatch, phase):
    # Given a real reader held at each ownership transition, retained across 500 transfers.
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"a" * 131072)
    original_open = backend.open_read
    original_read, original_verify, original_close = LocalReader.read_range, LocalReader.verify, LocalReader.close
    entered, release = Event(), Event()
    premature = []
    held = set()
    lock = Lock()

    def gate(reader):
        with lock:
            held.add(reader._descriptor)
        entered.set()
        assert release.wait(10)
        with lock:
            held.remove(reader._descriptor)

    def acquire(key):
        reader = original_open(key)
        if phase == "acquire":
            gate(reader)
        return reader

    def read(reader, offset, length):
        if phase == "prefetch" or (phase == "midstream" and offset):
            gate(reader)
        return original_read(reader, offset, length)

    def verify(reader):
        if phase == "head":
            gate(reader)
        return original_verify(reader)

    def close(reader):
        with lock:
            if reader._descriptor in held:
                premature.append(reader._descriptor)
        if phase == "close" and not reader._closed:
            gate(reader)
        original_close(reader)

    monkeypatch.setattr(backend, "open_read", acquire)
    monkeypatch.setattr(LocalReader, "read_range", read)
    monkeypatch.setattr(LocalReader, "verify", verify)
    monkeypatch.setattr(LocalReader, "close", close)
    scope = {"type": "http", "method": "HEAD" if phase in ("head", "close") else "GET", "headers": []}
    baseline = len(os.listdir("/proc/self/fd"))

    async def transfer():
        response = await media_streaming.stream_media(backend, "object", Request(scope), "video/mp4")
        if phase == "midstream":
            async for _ in response.body_iterator:
                pass

    # When native cancellation is repeated while the blocking worker still owns its resource.
    for _ in range(500):
        entered.clear()
        release.clear()
        task = asyncio.create_task(transfer())
        assert await anyio.to_thread.run_sync(entered.wait, 10)
        task.cancel()
        await anyio.lowlevel.checkpoint()
        task.cancel()
        await anyio.lowlevel.checkpoint()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    # Then neither GC nor an early close can hide a leak or use-after-close.
    assert premature == []
    for reader in backend.readers:
        assert_closed(reader)
    assert len(os.listdir("/proc/self/fd")) == baseline
    assert media_streaming._io._limiter.get().borrowed_tokens == 0


@pytest.mark.asyncio
async def test_cancelled_acquisitions_keep_four_worker_capacity_until_completion(tmp_path, monkeypatch):
    # Given four workers blocked after acquiring real readers.
    backend = RetainedBackend(tmp_path)
    (tmp_path / "object").write_bytes(b"data")
    original = backend.open_read
    release, four = Event(), Event()
    active = peak = 0
    lock = Lock()

    def acquire(key):
        nonlocal active, peak
        reader = original(key)
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                four.set()
        assert release.wait(10)
        with lock:
            active -= 1
        return reader

    monkeypatch.setattr(backend, "open_read", acquire)
    scope = {"type": "http", "method": "HEAD", "headers": []}
    tasks = [asyncio.create_task(media_streaming.stream_media(backend, "object", Request(scope), "video/mp4"))
             for _ in range(12)]
    assert await anyio.to_thread.run_sync(four.wait, 5)
    # When cancellation tries to release reservations while the first workers remain held.
    for task in tasks:
        task.cancel()
    for _ in range(20):
        await anyio.lowlevel.checkpoint()
    borrowed = media_streaming._io._limiter.get().borrowed_tokens
    release.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Then capacity was held until worker completion and every acquired reader is closed.
    assert borrowed == 4
    assert peak <= 4
    for reader in backend.readers:
        assert_closed(reader)
