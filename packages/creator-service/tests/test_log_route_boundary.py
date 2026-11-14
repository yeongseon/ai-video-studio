import pytest
from creator_service.error_redaction import redact_log_text


@pytest.mark.parametrize("text", [
    "uploads/api/admin/Q3_layoffs_board_deck", "data/api/creator/x",
    "C:/api/creator/x", "user:pass@host/api/creator/x",
])
def test_route_suffix_is_hidden_when_left_boundary_is_private(text: str) -> None:
    # Given / When a filesystem or authority prefix precedes a route-like suffix.
    result = redact_log_text(text)
    # Then the suffix cannot be mistaken for an independent route.
    assert result == "<redacted>"


@pytest.mark.parametrize("template", ["{}", "prefix {}", "GET {}", "'{}'", '"{}"'])
def test_route_survives_when_left_boundary_is_legitimate(template: str) -> None:
    # Given a normal route at an independent message boundary.
    text = template.format("/api/creator/runs/42")
    # When
    result = redact_log_text(text)
    # Then
    assert result == text
