"""Same-origin bounded delivery after callers establish asset ownership."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from functools import partial

import anyio
from creator_service.blocking_io import BlockingIO
from creator_service.object_storage import ArtifactStorageBackend, StorageObjectMissing, StorageReadError
from creator_service.object_storage_reads import READ_CHUNK_SIZE
from creator_service.storage_read_session import ObjectReader
from fastapi import HTTPException, Request
from starlette.responses import Response, StreamingResponse

logger = logging.getLogger(__name__)
_io = BlockingIO(4)


async def _run[T](function: Callable[[], T], discard: Callable[[T], None] | None = None) -> T:
    # Native Task.cancel bypasses CancelScope; this child retains the worker's
    # limiter reservation while repeated cancellation affects only the waiter.
    task = asyncio.create_task(_io.run(function))
    cancelled = False
    abandoned: list[T] = []

    def completed(done: asyncio.Task[T]) -> None:
        if discard is not None and not done.cancelled() and done.exception() is None:
            abandoned.append(done.result())

    task.add_done_callback(completed)
    with anyio.CancelScope(shield=True):
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
            except (StorageReadError, StorageObjectMissing):
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise
        if cancelled:
            for value in abandoned:
                if discard is not None:
                    await _run(partial(discard, value))
            raise asyncio.CancelledError()
        return result


def _range(header: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", header)
    if match is None or not any(match.groups()) or size == 0:
        raise ValueError("Invalid byte range")
    left, right = match.groups()
    if left:
        start = int(left)
        end = min(int(right) + 1, size) if right else size
    else:
        suffix = int(right)
        if suffix == 0:
            raise ValueError("Invalid byte range")
        start, end = max(0, size - suffix), size
    if start >= size or start >= end:
        raise ValueError("Unsatisfiable byte range")
    return start, end


def _failure(error: StorageObjectMissing | StorageReadError) -> HTTPException:
    if isinstance(error, StorageObjectMissing):
        return HTTPException(404, "Asset not found")
    # Deliberately omit exception/cause text, keys and provider URLs from logs.
    logger.error("Media storage read failed", extra={"failure_kind": error.kind})
    return HTTPException(503 if error.kind == "timeout" else 502, "Media storage unavailable")


async def stream_media(backend: ArtifactStorageBackend, key: str, request: Request, mime: str) -> Response:
    reader = None
    transferred = False
    try:
        try:
            with anyio.CancelScope(shield=True):
                reader = await _run(partial(backend.open_read, key), lambda acquired: acquired.close())
        except (StorageObjectMissing, StorageReadError) as error:
            raise _failure(error) from None
        await anyio.lowlevel.checkpoint()
        response = await _prepare_media(reader, request, mime)
        transferred = isinstance(response, OwnedMediaResponse)
        return response
    finally:
        if reader is not None and not transferred:
            with anyio.CancelScope(shield=True):
                await _run(reader.close)


class OwnedMediaResponse(StreamingResponse):
    def __init__(self, content: AsyncIterator[bytes], reader: ObjectReader, **kwargs) -> None:
        super().__init__(content, **kwargs)
        self._reader = reader

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await _run(self._reader.close)


async def _prepare_media(reader: ObjectReader, request: Request, mime: str) -> Response:
    try:
        info = reader.info
        if info.size < 0:
            raise StorageReadError()
    except (StorageObjectMissing, StorageReadError) as error:
        raise _failure(error) from None
    headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store",
               "Accept-Ranges": "bytes"}
    start, end, status = 0, info.size, 200
    range_header = request.headers.get("range")
    if request.method == "GET" and range_header is not None and "if-range" not in request.headers:
        try:
            start, end = _range(range_header, info.size)
        except ValueError:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{info.size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{info.size}"
    headers["Content-Length"] = str(end - start)
    if request.method == "HEAD" or start == end:
        try:
            await _run(reader.verify)
        except (StorageObjectMissing, StorageReadError) as error:
            raise _failure(error) from None
        return Response(media_type=mime, headers=headers)

    async def read(offset: int) -> bytes:
        length = min(READ_CHUNK_SIZE, end - offset)
        block = await _run(partial(reader.read_range, offset, length))
        if len(block) != length:
            raise StorageReadError()
        await _run(reader.verify)
        return block

    # Prefetch only one bounded block so initial provider failures retain HTTP
    # error semantics. Each read releases its response before returning bytes.
    try:
        first = await read(start)
    except (StorageObjectMissing, StorageReadError) as error:
        raise _failure(error) from None

    async def content() -> AsyncIterator[bytes]:
        try:
            yield first
            offset = start + len(first)
            while offset < end:
                await anyio.lowlevel.checkpoint()
                try:
                    block = await read(offset)
                except (StorageObjectMissing, StorageReadError) as error:
                    _failure(error)
                    raise StorageReadError() from None
                yield block
                offset += len(block)
        finally:
            with anyio.CancelScope(shield=True):
                await _run(reader.close)

    return OwnedMediaResponse(content(), reader, status_code=status, media_type=mime, headers=headers)
