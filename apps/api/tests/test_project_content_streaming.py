from datetime import datetime, timezone
from threading import Event

import anyio
from creator_domain.models import Project
from httpx import AsyncClient
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from shorts_api.auth import CurrentUser
from tests.project_preview_support import PreviewFixture, content_url, preview as preview

pytestmark = pytest.mark.asyncio


async def test_huge_preview_is_lazy_bounded_and_stops_on_cancellation(preview: PreviewFixture) -> None:
    # Given a 500 MiB object that only synthesizes requested chunks.
    from shorts_api.routes.creator_timeline import get_project_asset_content

    preview.backend.size = 500 * 1024 * 1024
    asset = await preview.asset()
    now = datetime.now(timezone.utc)
    access = (CurrentUser(user_id=1, workspace_id=7), Project(
        id=42, workspace_id=7, created_at=now, updated_at=now,
    ))
    request = Request({"type": "http", "method": "GET", "headers": []})
    response = await get_project_asset_content(42, asset, request=request, access=access)
    assert isinstance(response, StreamingResponse)
    assert len(preview.backend.reads) <= 1
    received: list[bytes] = []
    # When the consumer disconnects after the first chunk.
    with anyio.CancelScope() as scope:
        async for block in response.body_iterator:
            assert isinstance(block, bytes)
            received.append(block)
            scope.cancel()
            await anyio.lowlevel.checkpoint()
    await response.body_iterator.aclose()
    # Then no full buffering, further reads, or open provider response remains.
    assert len(received) == 1
    assert len(received[0]) == 65536
    assert len(preview.backend.reads) == 1
    assert preview.backend.active == 0


async def test_slow_preview_storage_does_not_block_health(
    client: AsyncClient, preview: PreviewFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given a backend blocked on an event rather than a timing-dependent sleep.
    entered, release = Event(), Event()
    original = preview.backend.read_range

    def slow_read(key: str, offset: int, length: int) -> bytes:
        entered.set()
        assert release.wait(5), "health could not run while storage was blocked"
        return original(key, offset, length)

    monkeypatch.setattr(preview.backend, "read_range", slow_read)
    asset = await preview.asset()

    async def download() -> None:
        response = await client.get(content_url(asset))
        assert response.status_code == 200
        assert response.content == preview.backend.data

    # When media I/O and health run concurrently.
    async with anyio.create_task_group() as group:
        group.start_soon(download)
        try:
            assert await anyio.to_thread.run_sync(entered.wait, 5)
            with anyio.fail_after(1):
                health = await client.get("/healthz")
            assert health.status_code == 200
        finally:
            release.set()


async def test_asgi_disconnect_stops_bounded_reads(preview: PreviewFixture) -> None:
    # Given a large representation and the real StreamingResponse ASGI surface.
    from shorts_api.routes.creator_timeline import get_project_asset_content

    preview.backend.size = 500 * 1024 * 1024
    asset = await preview.asset()
    now = datetime.now(timezone.utc)
    access = (CurrentUser(user_id=1, workspace_id=7), Project(
        id=42, workspace_id=7, created_at=now, updated_at=now,
    ))
    scope = {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.3"}}
    response = await get_project_asset_content(42, asset, request=Request(scope), access=access)
    sent = anyio.Event()

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sent.set()
            await anyio.sleep_forever()

    async def receive():
        await sent.wait()
        return {"type": "http.disconnect"}

    # When the ASGI client disconnects while its first body chunk is sent.
    with anyio.fail_after(2):
        await response(scope, receive, send)
    # Then cancellation completes with no later fetch or live provider response.
    assert preview.backend.reads == [(0, 65536)]
    assert preview.backend.active == 0
