"""Synthetic opaque route data must not cross the JSON logging boundary."""

import json
import logging

import pytest
from creator_service.logging_config import JsonFormatter


@pytest.mark.parametrize("segment", [
    "YWRtaW46cGFzc3dvcmQ", "xsk-ABCDEFGHIJ", "x_hf_abcdefgh", "HF_abcdef",
    "SK-abcdefghij", "a" * 19, "slug_42-a", "a" * 12, "a" * 25,
    "123456789012345678901", "xeyjword", "xhf_word", "xr8_word", "xsk-word",
    "xsk_word", "xgskword", "xghpword", "xxoxbword", "xakiaword", "xaizaword",
    "xpplx-word",
])
def test_opaque_segment_hides_entire_tail_when_route_is_logged(segment: str) -> None:
    # Given an opaque value followed by another possible part of a slash-bearing secret.
    path = f"/api/creator/runs/1/{segment}/private_tail/"
    record = logging.makeLogRecord({"msg": path, "detail": {"path": path}})
    # When the real formatter handles the message and nested extras.
    result = json.loads(JsonFormatter("api").format(record))
    # Then only the safe prefix remains, with one marker for the entire unsafe tail.
    assert result["message"] == "/api/creator/runs/1/<redacted>"
    assert result["detail"] == {"path": "/api/creator/runs/1/<redacted>"}
    assert segment not in result["message"]
    assert "private_tail" not in result["message"]


@pytest.mark.parametrize("segment", [
    "0", "12345678901234567890", "a", "route_word-with-hyphens", "z" * 24,
    "a" * 11, "feed", "workspaces",
])
def test_safe_segment_survives_when_route_is_logged(segment: str) -> None:
    # Given a segment on the accepted numeric or lowercase route-word boundary.
    path = f"/api/creator/{segment}/"
    # When
    result = json.loads(JsonFormatter("api").format(logging.makeLogRecord({"msg": path})))
    # Then
    assert result["message"] == path


def test_slug_tail_is_hidden_when_authorised_safe_fixture_is_reclassified() -> None:
    # Given the previously accepted digit-bearing slug (D1c).
    path = "/api/creator/projects/slug_42-a/"
    # When
    result = json.loads(JsonFormatter("api").format(logging.makeLogRecord({"msg": path})))
    # Then
    assert result["message"] == "/api/creator/projects/<redacted>"
    assert "slug_42-a" not in result["message"]
