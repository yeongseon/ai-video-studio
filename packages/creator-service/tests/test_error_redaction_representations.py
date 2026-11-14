import json
from collections.abc import Callable

import pytest

from creator_service.error_redaction import redact_error_message, redact_log_text


@pytest.mark.parametrize("redact", [redact_error_message, redact_log_text])
@pytest.mark.parametrize(("text", "expected"), [
    ("password=b'hunter2secret'", "password=<redacted>"),
    ("{'password': b'hunter2secret'}", "{'password': <redacted>}"),
    ("password: u'hunter2'", "password: <redacted>"),
    ("{b'password': b'hunter2secret'}", "{b'password': <redacted>}"),
    ("{'token': b'abc123'}", "{'token': <redacted>}"),
    ('BR"password": rb"synthetic secret"', 'BR"password": <redacted>'),
    (r'"password": "a\"bsecret"', '"password": "<redacted>"'),
    (json.dumps({"password": 'p"ss"word'}), '{"password": "<redacted>"}'),
    (r"password='a\'bsecret'", "password='<redacted>'"),
    ("password='unterminated\\'secret", "password='<redacted>"),
    ("password=b'unterminated\\'secret", "password=<redacted>"),
    (r'password="ends\\" ordinary', 'password="<redacted>" ordinary'),
])
def test_quoted_representation_is_fully_hidden_when_prefixes_or_escapes_are_present(
    redact: Callable[[str], str], text: str, expected: str,
) -> None:
    # Given / When
    result = redact(text)
    # Then
    assert result == expected


@pytest.mark.parametrize("redact", [redact_error_message, redact_log_text])
@pytest.mark.parametrize("name", ["Authorization", "proxy-authorization", "x-authorization"])
@pytest.mark.parametrize("value", [
    "Basic dXNlcjpwYXNz", "Token abc", "AWS4-HMAC-SHA256 Credential=synthetic, Signature=synthetic",
    "Bearer x",
])
def test_authorization_hides_entire_scheme_value_when_header_is_unquoted(
    redact: Callable[[str], str], name: str, value: str,
) -> None:
    # Given
    text = f"{name}: {value}\nordinary diagnostic"
    # When
    result = redact(text)
    # Then
    assert result == f"{name}: <redacted>\nordinary diagnostic"
