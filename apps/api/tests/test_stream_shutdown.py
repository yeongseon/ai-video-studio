import asyncio
import subprocess
import sys
import os
from pathlib import Path
from threading import Event

import anyio
import pytest

from shorts_api import media_streaming


@pytest.mark.parametrize("cancel_child", [False, True])
def test_loop_shutdown_releases_held_acquisition_without_hanging(tmp_path, cancel_child):
    script = '''
import asyncio, errno, os, sys
from threading import Event, Thread
from creator_service.object_storage import LocalStorageBackend
from shorts_api.media_streaming import _run
entered, release = Event(), Event()
backend = LocalStorageBackend(sys.argv[1])
readers = []
def acquire():
    reader = backend.open_read("object")
    readers.append(reader)
    entered.set()
    assert release.wait(5)
    return reader
async def main():
    waiter = asyncio.create_task(_run(acquire, lambda reader: reader.close()))
    while not entered.is_set():
        await asyncio.sleep(0)
    if sys.argv[2] == "True":
        for task in asyncio.all_tasks():
            if task not in (asyncio.current_task(), waiter):
                task.cancel()
    def finish():
        release.set()
    asyncio.get_running_loop().call_soon(finish)
asyncio.run(main())
assert readers and readers[0]._closed
try:
    os.fstat(readers[0]._descriptor)
except OSError as error:
    assert error.errno == errno.EBADF
else:
    raise AssertionError("retained descriptor leaked")
'''
    (tmp_path / "object").write_bytes(b"data")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(cancel_child)],
        capture_output=True, text=True, timeout=8,
        env={**os.environ, "PYTHONPATH": str(Path(media_streaming.__file__).parents[1])},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_cancelled_waiter_takes_precedence_over_worker_value_error():
    entered, release = Event(), Event()

    def failed():
        entered.set()
        assert release.wait(5)
        raise ValueError("private-worker-marker")

    task = asyncio.create_task(media_streaming._run(failed))
    assert await anyio.to_thread.run_sync(entered.wait, 5)
    task.cancel()
    await anyio.lowlevel.checkpoint()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_worker_exception_is_transported_to_waiter():
    error = MemoryError("private-worker-marker")

    def failed():
        raise error

    with pytest.raises(MemoryError) as raised:
        await media_streaming._run(failed)
    assert raised.value is error


@pytest.mark.parametrize("error_type", ["KeyboardInterrupt", "SystemExit"])
def test_worker_control_exception_cancels_waiter_without_transport(error_type):
    script = '''
import asyncio, builtins, sys
from concurrent.futures import ThreadPoolExecutor
from shorts_api.media_thread_io import MediaThreadIO
error_type = getattr(builtins, sys.argv[1])
class RecordingExecutor(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=1)
        self.submitted = []
    def submit(self, function, *args, **kwargs):
        future = super().submit(function, *args, **kwargs)
        self.submitted.append(future)
        return future
io = MediaThreadIO()
io.executor.shutdown()
io.executor = RecordingExecutor()
def failed():
    raise error_type("private-worker-marker")
async def main():
    try:
        await io.run(failed)
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("pending waiter was not cancelled")
    assert isinstance(io.executor.submitted[0].exception(), error_type)
    assert await io.run(lambda: 42) == 42
    assert io.borrowed_tokens == 0
asyncio.run(main())
io.executor.shutdown()
'''
    result = subprocess.run([sys.executable, "-c", script, error_type], capture_output=True, text=True, timeout=8,
                            env={**os.environ, "PYTHONPATH": str(Path(media_streaming.__file__).parents[1])})
    assert result.returncode == 0, result.stderr
