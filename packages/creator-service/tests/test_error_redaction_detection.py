"""Credential detection contracts; all values are synthetic."""

from collections.abc import Callable
from time import perf_counter

import pytest

from creator_service.error_redaction import (
    carries_credential,
    is_sensitive_log_key,
    redact_error_message,
    redact_log_text,
)


@pytest.fixture(params=[redact_error_message, redact_log_text], ids=["error", "log"])
def redact(request: pytest.FixtureRequest) -> Callable[[str], str]:
    return request.param


ISSUE_CASES = [
    ("token=hunter2supersecret", "token=<redacted>"),
    ("password=correct-horse-battery", "password=<redacted>"),
    ("{'x-api-key': 'a1b2c3d4e5f6'}", "{'x-api-key': '<redacted>'}"),
    ("Invalid credentials for hf_AbCdEfGhIjKlMnOpQr", "Invalid credentials for <redacted>"),
    ("replicate r8_AbCdEfGhIjKlMnOpQrStUv rejected", "replicate <redacted> rejected"),
]


@pytest.mark.parametrize(("text", "expected"), ISSUE_CASES)
def test_issue_credentials_are_redacted(
    redact: Callable[[str], str], text: str, expected: str,
) -> None:
    # Given / When
    result = redact(text)
    # Then
    assert result == expected


@pytest.mark.parametrize("name", [
    "key", "api_key", "x-api-key", "accessToken", "client_secret", "password_policy",
    "passwd", "pwd", "passphrase", "authorization", "cookie", "credentials",
])
def test_named_values_use_the_log_key_predicate(
    redact: Callable[[str], str], name: str,
) -> None:
    # Given
    assert is_sensitive_log_key(name)
    # When
    result = redact(f'{name}="synthetic value, with; separators&[]"')
    # Then
    assert result == f'{name}="<redacted>"'


@pytest.mark.parametrize("separator", [" ", "\t", ",", ";", "&", "'", '"', "[", "]", "{", "}", "(", ")"])
def test_unquoted_value_stops_at_delimiter(
    redact: Callable[[str], str], separator: str,
) -> None:
    # Given
    text = f"token=synthetic{separator}ordinary"
    # When
    result = redact(text)
    # Then
    assert result == f"token=<redacted>{separator}ordinary"


@pytest.mark.parametrize(("text", "expected"), [
    ('{"password": "synthetic secret"}', '{"password": "<redacted>"}'),
    ("'x-api-key': 'a1b2c3'", "'x-api-key': '<redacted>'"),
    ("token = 'synthetic\"secret' next=5", "token = '<redacted>' next=5"),
    ('message="token=synthetic"', 'message="token=<redacted>"'),
    ("token='unterminated secret", "token='<redacted>"),
    ("token=abc=def:ghi", "token=<redacted>"),
    ('"session key": "synthetic value"', '"session key": "<redacted>"'),
    ("'private key': 'synthetic value'", "'private key': '<redacted>'"),
    ("123_token=synthetic", "123_token=<redacted>"),
    ("authorization: Bearer synthetic-value", "authorization: <redacted>"),
    ("token=", "token="),
])
def test_named_value_boundaries(
    redact: Callable[[str], str], text: str, expected: str,
) -> None:
    # Given / When
    result = redact(text)
    # Then
    assert result == expected


NEGATIVE_CORPUS = [
    "token_count=5", "key_id=7", "max_tokens=1000", "monkey=banana",
    "key_fingerprint=abc123", "storage_key=scene-42", "idempotency_key=request-7",
    "cache_key=image-42", "model_key=flux", "tokenizer=local", "tokens=42",
    "hf_short r8_tiny", "hf_abcde r8_abcde", "prefixhf_AbCdEfGhIjKlMnOpQr",
    "timeline-42", "run_701", "550e8400-e29b-41d4-a716-446655440000",
    "Resource timeline-42 not found", "Invalid input", "Run not found",
    "Version conflict: expected=2 actual=3", "Provider unavailable; retry later",
    "HuggingFace returned non-image content: text/plain", "GROQ_API_KEY not set",
    'File "worker.py", line 42, in generate_script', "ValueError: invalid scene count",
    "Traceback (most recent call last):", "raise RuntimeError('render failed')",
    "worker.py:42", r"C:relative_file.py", "scene.png", "./worker.py",
    "SELECT run_id FROM runs WHERE key_id=7;", "UPDATE runs SET token_count=5;",
    '{"run_id": 42, "token_count": 5, "key_id": 7}',
    '{"message": "Invalid input", "max_tokens": 1000}',
    "timeout=30; retries=3", "content_type=application/json", "status: failed",
    "notpassword=ordinary", "mytoken=ordinary", "token_count : 5",
]


@pytest.mark.parametrize("text", NEGATIVE_CORPUS)
def test_negative_corpus_is_unchanged(redact: Callable[[str], str], text: str) -> None:
    # Given / When
    result = redact(text)
    # Then
    assert result == text


@pytest.mark.parametrize("text", [case[0] for case in ISSUE_CASES] + [
    'token=<redacted> password="<redacted>"',
    "token=synthetic; password='another synthetic value'",
])
def test_redaction_is_idempotent(redact: Callable[[str], str], text: str) -> None:
    # Given
    once = redact(text)
    # When
    twice = redact(once)
    # Then
    assert twice == once


@pytest.mark.parametrize("text", [case[0] for case in ISSUE_CASES])
def test_frame_credential_check_uses_new_detection(text: str) -> None:
    # Given / When
    result = carries_credential(text)
    # Then
    assert result is True


# Short ids keep megabyte-sized inputs out of test node ids: verbose CI output otherwise
# prints each input as one enormous log line and the runner stalls for minutes on it.
PATHOLOGICAL_INPUTS = {
    "equals-100k": "=" * 100_000,
    "colons-100k": ":" * 100_000,
    "quotes-200k": "'\"" * 100_000,
    "token-500k": "token" * 100_000,
    "a-1MiB": "a" * 1_048_576,
    "token-assign-8k": "token=" + "a" * 8000,
    "token-quote-8k": "token='" + "a" * 8000,
    "token-colon-2k": "token" * 400 + ":",
    "quote-2k": "'" * 2000,
    "equals-2k": "=" * 2000,
    "colon-2k": ":" * 2000,
    "token-lines-100k": "token=x\n" * 100_000,
}


@pytest.mark.parametrize("text", PATHOLOGICAL_INPUTS.values(), ids=PATHOLOGICAL_INPUTS.keys())
def test_pathological_input_is_bounded(redact: Callable[[str], str], text: str) -> None:
    # Given
    start = perf_counter()
    # When
    result = redact(text)
    elapsed = perf_counter() - start
    # Then: an intentionally generous CI bound, with pre-existing output budgets.
    assert elapsed < 2.0
    assert len(result.encode()) <= 65536
    assert len(result.splitlines()) <= 401
    assert all(len(line) <= 1024 for line in result.splitlines())
    if len(text) > 8192 and redact is redact_error_message:
        assert result == "<redacted>"
