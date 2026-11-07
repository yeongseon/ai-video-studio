from httpx import AsyncClient
import pytest

from tests.project_preview_support import PreviewFixture, content_url, preview as preview

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("header,status,start,end", [
    (None, 200, 0, 23), ("bytes=2-5", 206, 2, 6), ("bytes=6-", 206, 6, 23),
    ("bytes=-4", 206, 19, 23), ("bytes=0-999", 206, 0, 23), ("bytes=-999", 206, 0, 23),
    ("bytes=999-", 416, 0, 0), ("bytes=5-2", 416, 0, 0), ("bytes=-0", 416, 0, 0),
    ("bytes=nope", 416, 0, 0), ("bytes=1-2,4-5", 416, 0, 0), ("items=0-1", 416, 0, 0),
])
async def test_project_content_range_contract(
    client: AsyncClient, preview: PreviewFixture, header: str | None, status: int, start: int, end: int,
) -> None:
    # Given a known remote representation.
    preview.backend.data = b"0123456789abcdefghijklm"
    preview.backend.size = 23
    asset = await preview.asset()
    # When seeking with a single range, or a malformed/unsatisfiable range.
    response = await client.get(content_url(asset), headers={"Range": header} if header else {})
    # Then both bytes and seek metadata agree exactly.
    assert response.status_code == status
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    if status == 416:
        assert response.headers["content-range"] == "bytes */23"
        assert preview.backend.reads == []
    else:
        assert response.content == preview.backend.data[start:end]
        assert response.headers["content-length"] == str(end - start)
        if status == 206:
            assert response.headers["content-range"] == f"bytes {start}-{end - 1}/23"
        else:
            assert "content-range" not in response.headers


async def test_head_stats_but_never_reads(client: AsyncClient, preview: PreviewFixture) -> None:
    # Given an owned object.
    asset = await preview.asset()
    # When HEAD includes a range (ranges only apply to GET).
    response = await client.head(content_url(asset), headers={"Range": "bytes=1-2"})
    # Then full representation headers are returned without reading bytes.
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == str(preview.backend.size)
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["accept-ranges"] == "bytes"
    assert preview.backend.calls == 1
    assert preview.backend.reads == []


async def test_if_range_falls_back_to_full_representation(client: AsyncClient, preview: PreviewFixture) -> None:
    # Given no validator support; an If-Range validator cannot match.
    asset = await preview.asset()
    # When a client asks to seek conditionally.
    response = await client.get(content_url(asset), headers={"Range": "bytes=1-2", "If-Range": '"old"'})
    # Then it receives the full current representation, never a stale partial response.
    assert response.status_code == 200
    assert response.content == preview.backend.data


async def test_empty_object_has_full_response_without_reads(client: AsyncClient, preview: PreviewFixture) -> None:
    # Given an empty representation.
    preview.backend.size = 0
    asset = await preview.asset()
    # When requested in full.
    response = await client.get(content_url(asset))
    # Then it does not invent an invalid backend byte range.
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert preview.backend.reads == []
