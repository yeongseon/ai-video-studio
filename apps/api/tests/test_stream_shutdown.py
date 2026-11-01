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


@pytest.mark.parametrize("error_type", ["KeyboardInterrupt", "SystemExit", "MemoryError"])
def test_worker_base_exception_does_not_corrupt_completion(error_type):
    script = '''
import asyncio, builtins, sys
from shorts_api.media_streaming import _run
error_type = getattr(builtins, sys.argv[1])
def failed():
    raise error_type("private-worker-marker")
async def main():
    try:
        await _run(failed)
    except error_type:
        pass
    else:
        raise AssertionError("worker exception was lost")
    assert await _run(lambda: 42) == 42
asyncio.run(main())
'''
    result = subprocess.run([sys.executable, "-c", script, error_type], capture_output=True, text=True, timeout=8,
                            env={**os.environ, "PYTHONPATH": str(Path(media_streaming.__file__).parents[1])})
    assert result.returncode == 0, result.stderr
    assert "private-worker-marker" not in result.stderr
