from collections.abc import Iterator
from contextlib import contextmanager
from threading import Event, Thread
import socket

from fastapi import FastAPI, Request
import httpx
import uvicorn

from shorts_api.media_streaming import stream_media


@contextmanager
def http_stream(backend, key: str) -> Iterator[httpx.Client]:
    application = FastAPI()

    @application.get("/content")
    @application.head("/content")
    async def content(request: Request):
        return await stream_media(backend, key, request, "video/mp4")

    ready = Event()

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = Server(uvicorn.Config(application, lifespan="off", log_config=None, access_log=False))
        worker = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        worker.start()
        assert ready.wait(5)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=5) as client:
                yield client
        finally:
            server.should_exit = True
            worker.join(10)
            assert not worker.is_alive()
