"""Thread-owned completion for previews, independent of cancellable loop tasks."""

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock
from typing import Generic, TypeVar

import anyio
from anyio.lowlevel import RunVar

T = TypeVar("T")


class Completion(Generic[T]):
    def __init__(self, discard: Callable[[T], None] | None) -> None:
        self.lock = Lock()
        self.discard = discard
        self.abandoned = False
        self.claimed = False
        self.finished = False
        self.discarded = False
        self.value: T | None = None
        self.error: BaseException | None = None

    def complete(self, future: Future[tuple[T | None, BaseException | None]]) -> None:
        with self.lock:
            self.value, self.error = future.result()
            self.finished = True
            if self.abandoned and self.error is None and self.discard is not None:
                self.discarded = True
                self.discard(self.value)

    def abandon(self) -> None:
        with self.lock:
            if not self.abandoned:
                self.abandoned = True
                if self.finished and not self.claimed and not self.discarded and self.error is None and self.discard is not None:
                    self.discarded = True
                    self.discard(self.value)

    def claim(self) -> T:
        with self.lock:
            self.claimed = True
            if self.error is not None:
                raise self.error
            return self.value


class MediaThreadIO:
    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="media-preview")
        self.lock = Lock()
        self.borrowed_tokens = 0
        self._limiter: RunVar[MediaThreadIO] = RunVar("media_preview_accounting")

    async def run(self, function: Callable[[], T], discard: Callable[[T], None] | None = None) -> T:
        self._limiter.set(self)
        completion = Completion(discard)
        context = copy_context()

        def worker() -> tuple[T | None, BaseException | None]:
            with self.lock:
                self.borrowed_tokens += 1
            try:
                return context.run(function), None
            except BaseException as error:
                # Thread boundary: carry even process-control exceptions as data,
                # not asyncio Task exceptions that can terminate the event loop.
                return None, error

        future = self.executor.submit(worker)

        def completed(done: Future[tuple[T | None, BaseException | None]]) -> None:
            try:
                completion.complete(done)
            finally:
                with self.lock:
                    self.borrowed_tokens -= 1

        future.add_done_callback(completed)
        wrapped = asyncio.wrap_future(future)
        cancelled = False
        with anyio.CancelScope(shield=True):
            while not wrapped.done():
                try:
                    await asyncio.shield(wrapped)
                except asyncio.CancelledError:
                    cancelled = True
                    completion.abandon()
            if cancelled:
                raise asyncio.CancelledError() from None
            return completion.claim()
