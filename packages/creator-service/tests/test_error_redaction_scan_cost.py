from itertools import product
from time import perf_counter

import pytest

from creator_service import error_redaction as redaction


def test_scanner_avoids_predicate_when_assignment_names_have_no_sensitive_vocabulary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the real classifier instrumented only to count expensive calls.
    predicate = redaction.is_sensitive_log_key
    calls: list[str] = []

    def counted(name: str) -> bool:
        calls.append(name)
        return predicate(name)

    monkeypatch.setattr(redaction, "is_sensitive_log_key", counted)
    text = "token_count=5;" + "a=" * 4096
    # When
    result = redaction._redact_named_values(text)
    # Then only the vocabulary-bearing assignment needs the full classifier.
    assert result == text
    assert calls == ["token_count"]


@pytest.mark.parametrize("fragment", ["'a ", "token:'", "a-", "a=", "password='"])
def test_scanner_finishes_when_adversarial_input_bypasses_public_caps(fragment: str) -> None:
    # Given approximately one MiB delivered directly to the assignment scanner.
    text = "token_count=5;" + fragment * (1_048_576 // len(fragment))
    # When
    start = perf_counter()
    result = redaction._redact_named_values(text)
    elapsed = perf_counter() - start
    # Then a quadratic regression would exceed this generous bound by minutes.
    assert elapsed < 5.0, f"{fragment!r}: {elapsed:.3f}s"
    assert len(result) <= len(text) * 2


@pytest.mark.parametrize("redact, text, expected", [
    (redaction.redact_error_message, "a=" * 4096, "a=" * 512),
    (redaction.redact_log_text, "a=" * 1023, "a=" * 512),
    (redaction.redact_log_text, "\n".join(["a=" * 1023] * 400),
     "\n".join(["a=" * 512] * 63 + ["<output truncated>"])),
    (redaction.carries_credential, "a=" * 1024, False),
])
def test_public_scan_finishes_when_equals_dense_input_is_under_cap(redact, text: str, expected) -> None:
    # Given / When
    start = perf_counter()
    result = redact(text)
    elapsed = perf_counter() - start
    # Then
    assert elapsed < 2.0
    assert result == expected


def test_prefilter_has_no_false_negatives_when_predicate_vocabulary_is_composed() -> None:
    # Given sensitive vocabulary, camel case, segment separators and unrelated names.
    words = sorted(redaction._LOG_SECRETS | redaction._LOG_KEY_PREFIXES | {"key", "count", "name", "İ", "K"})
    names = {"key", "apikey", "x-api-key", "token_count", "key_fingerprint"}
    for first, second, separator in product(words, words, ["_", "-", ".", " ", ""]):
        names.update((first + separator + second, first.upper() + separator + second.title(),
                      first.title() + second.title()))
    # When / Then every positive predicate decision must reach the scanner's classifier.
    for name in names:
        if redaction.is_sensitive_log_key(name):
            assert redaction._SENSITIVE_NAME_HINT.search(name), name
