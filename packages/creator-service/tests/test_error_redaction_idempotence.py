import random
from collections.abc import Callable

import pytest

from creator_service.error_redaction import redact_error_message, redact_log_text


@pytest.mark.parametrize("redact", [redact_error_message, redact_log_text])
@pytest.mark.parametrize("text", ["hf_abcdefgh'api key':2x", "token=abc'password': x"])
def test_redaction_is_idempotent_when_replacement_changes_a_name_boundary(
    redact: Callable[[str], str], text: str,
) -> None:
    # Given
    once = redact(text)
    # When
    twice = redact(once)
    # Then
    assert twice == once


@pytest.mark.parametrize("redact", [redact_error_message, redact_log_text])
def test_redaction_is_idempotent_when_seeded_fragments_are_combined(
    redact: Callable[[str], str],
) -> None:
    # Given a reproducible corpus including nested assignments and replacement boundaries.
    rng = random.Random(1005)
    alphabet = ["token", "password", "api key", "Authorization", "name", "token_count",
                "=", ":", "'", '"', "b", "r", "u", "br", " ", "\t", "\n", "\\",
                "<redacted>", "hf_abcdefgh", "abc", "x", "[", "]", ",", ";", "-"]
    texts = ["".join(rng.choices(alphabet, k=rng.randrange(2, 45))) for _ in range(5000)]
    # When / Then
    for text in texts:
        once = redact(text)
        assert redact(once) == once, repr(text)
