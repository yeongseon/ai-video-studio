import io
import json
import logging
from urllib.parse import quote

import pytest
from creator_service.logging_config import JsonFormatter
from creator_service.uvicorn_logging import UvicornAccessFilter
from fastapi.testclient import TestClient
from shorts_api.app_factory import create_app, logger


@pytest.mark.parametrize("codepoint", [*range(32), 127, 0x2028, 0x2029], ids=lambda n: f"U+{n:04X}")
def test_access_path_escapes_control_when_request_target_is_percent_encoded(codepoint: int) -> None:
    # Given the production middleware and real access filter / JSON formatter.
    application = create_app()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(UvicornAccessFilter())
    handler.setFormatter(JsonFormatter("api"))
    logger.addHandler(handler)
    encoded = quote(chr(codepoint), safe="")
    # When a real request contains a percent-encoded control in its decoded path.
    try:
        with TestClient(application) as client:
            response = client.get(f"/x{encoded}forged-line")
    finally:
        logger.removeHandler(handler)
        handler.close()
    # Then the decoded JSON message has one visible path, not another forged line.
    assert response.status_code == 404
    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert len(records) == 1
    message = records[0]["message"]
    escape = f"\\x{codepoint:02x}" if codepoint < 128 else f"\\u{codepoint:04x}"
    assert message.startswith(f"GET /x{escape}forged-line 404 ")
    assert chr(codepoint) not in message


def test_access_path_is_unchanged_when_request_has_ordinary_unicode() -> None:
    # Given the same production log boundary with a normal Unicode single-segment path.
    application = create_app()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("api"))
    logger.addHandler(handler)
    # When
    try:
        with TestClient(application) as client:
            response = client.get("/café")
    finally:
        logger.removeHandler(handler)
        handler.close()
    # Then ordinary paths and status diagnostics remain unchanged.
    assert response.status_code == 404
    assert json.loads(stream.getvalue())["message"].startswith("GET /café 404 ")
