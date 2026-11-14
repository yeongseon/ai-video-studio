from collections.abc import Callable

import pytest

from creator_service.error_redaction import redact_error_message, redact_log_text


@pytest.mark.parametrize("redact", [redact_error_message, redact_log_text])
@pytest.mark.parametrize(("text", "expected"), [
    ("Invalid token: expired", "Invalid token: <redacted>"),
    ("key: not found", "key: <redacted> found"),
    ("password: String should have at least 8 characters", "password: <redacted> should have at least 8 characters"),
    ("credentials: invalid or expired", "credentials: <redacted> or expired"),
    ("Expected key: value pairs", "Expected key: <redacted> pairs"),
    ("token_type=bearer", "token_type=<redacted>"),
    ("password_hash=x", "password_hash=<redacted>"),
    ("claim_token = $3", "claim_token = <redacted>"),
    ("claim_token=NULL", "claim_token=<redacted>"),
    ("    token = settings.api_token", "    token = <redacted>"),
])
def test_sensitive_name_fails_closed_when_value_is_prose_sql_or_source(
    redact: Callable[[str], str], text: str, expected: str,
) -> None:
    # Given / When
    result = redact(text)
    # Then
    assert result == expected
