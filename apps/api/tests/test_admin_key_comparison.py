import ast
import inspect

import pytest
from shorts_api import lifecycle


@pytest.mark.parametrize(("provided", "expected", "matches"), [
    ("synthetic-key", "synthetic-key", True),
    ("wrong", "synthetic-key", False),
    ("Synthetic-key", "synthetic-key", False),
    (" synthetic-key", "synthetic-key", False),
    ("\xff", "synthetic-key", False),
    ("synthetic-key", "é", False),
    ("é", "é", True),
    ("Ã©", "é", False),
    ("\ud800", "\ud800", True),
    ("\ud800", "\ud801", False),
    ("\ud800", "?", False),
    ("\ud800", "\ufffd", False),
    ("?", "\ud800", False),
    ("", "", False),
    ("", "synthetic-key", False),
    (None, "synthetic-key", False),
    (None, "", False),
    ("synthetic-key", "", False),
    ("x" * 100_000, "x" * 100_000, True),
    ("x" * 100_000 + "é", "x" * 100_000, False),
])
def test_exact_match_when_keys_are_arbitrary_text(
    provided: str | None, expected: str, matches: bool,
) -> None:
    # Given decoded text, including inputs rejected by string compare_digest.
    # When the shared comparison runs.
    result = lifecycle.admin_key_matches(provided, expected)
    # Then only equal, nonempty decoded strings authenticate.
    assert result is matches


def test_constant_time_byte_comparison_when_keys_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a recording seam at the constant-time primitive.
    calls: list[tuple[bytes, bytes]] = []

    def compare(provided: bytes, expected: bytes) -> bool:
        calls.append((provided, expected))
        return False

    monkeypatch.setattr("hmac.compare_digest", compare)
    # When keys include non-ASCII text and a lone surrogate.
    result = lifecycle.admin_key_matches("é\ud800", "synthetic-key")
    # Then UTF-8/surrogatepass bytes reach the primitive and its decision is preserved.
    assert calls == [(b"\xc3\xa9\xed\xa0\x80", b"synthetic-key")]
    assert result is False


def test_constant_time_primitive_is_the_only_key_comparison() -> None:
    # Given the one implementation that owns the timing-safe guarantee.
    source = inspect.getsource(lifecycle.admin_key_matches)
    # When inspecting its comparison operations.
    tree = ast.parse(source)
    # Then key equality is never replaced with an ordinary string/byte comparison.
    assert "hmac.compare_digest" in source
    assert not any(isinstance(node, (ast.Eq, ast.NotEq)) for node in ast.walk(tree))
