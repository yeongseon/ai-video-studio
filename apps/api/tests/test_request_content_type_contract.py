"""Preserve the pre-upgrade JSON request contract for issue #1023 group 1."""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, ConfigDict
from shorts_api.app_factory import create_app


class JsonPayload(BaseModel):
    model_config = ConfigDict(frozen=True)
    value: str


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "application/json"}])
async def test_json_is_parsed_when_content_type_is_missing_or_json(headers: dict[str, str]) -> None:
    # Given the real application factory and a typed JSON body endpoint.
    app = create_app()

    @app.post("/test/content-type")
    def echo(payload: JsonPayload) -> JsonPayload:
        return payload

    # When a client sends JSON with the supplied headers (raw content adds none).
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/test/content-type", content=b'{"value":"synthetic"}', headers=headers,
        )

    # Then the body is parsed, rather than rejected or passed through as bytes.
    assert response.status_code == 200
    assert response.json() == {"value": "synthetic"}


async def test_json_is_rejected_when_content_type_is_text_plain() -> None:
    # Given the same typed-body contract through the real factory.
    app = create_app()

    @app.post("/test/content-type")
    def echo(payload: JsonPayload) -> JsonPayload:
        return payload

    # When a client explicitly labels its JSON bytes as plain text.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/test/content-type", content=b'{"value":"synthetic"}',
            headers={"Content-Type": "text/plain"},
        )

    # Then the baseline validation error remains a body/model error.
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "model_attributes_type"
    assert response.json()["detail"][0]["loc"] == ["body"]
