"""CPU and scan-budget regressions for untrusted log text."""

import json
import logging
from collections.abc import Callable
from time import perf_counter
from timeit import repeat

import pytest
from creator_service.error_redaction import carries_credential, redact_log_text
from creator_service.logging_config import JsonFormatter


@pytest.mark.parametrize("prefix", ["", "/", "https://"])
@pytest.mark.parametrize("redact", [redact_log_text, carries_credential])
@pytest.mark.parametrize("small,large", [(500, 2000), (2000, 8000)])
def test_log_scanning_scales_linearly_when_scheme_delimiter_is_missing(
    prefix: str, redact: Callable[[str], str | bool], small: int, large: int,
) -> None:
    # Given hostile scheme runs, both below and across the log-only line cap.
    inputs = [prefix + "a." * (size // 2) for size in (small, large)]

    # When timing repeated scans, use the minimum to discount scheduler interruptions.
    timings = [min(repeat(lambda: redact(text), number=3, repeat=5)) for text in inputs]

    # Then quadrupling input must not produce quadratic (roughly 16x) work.
    ratio = timings[1] / timings[0]
    assert ratio < 8, f"scan ratio {ratio:.2f}: {timings}"


def test_formatter_finishes_promptly_when_provider_body_has_400_hostile_lines() -> None:
    # Given the maximum retained line count with hostile, formerly scanned line lengths.
    body = "\n".join(["https://" + "a." * 4000] * 400)
    record = logging.makeLogRecord({"msg": body})
    formatter = JsonFormatter("api")

    # When the real JSON boundary formats the record synchronously.
    start = perf_counter()
    raw = formatter.format(record)
    elapsed = perf_counter() - start

    # Then it fails closed without monopolizing the event loop for seconds.
    assert elapsed < 1.5, f"400-line scan took {elapsed:.3f}s"
    assert json.loads(raw)["message"].splitlines()[:400] == ["<redacted>"] * 400


@pytest.mark.parametrize("length", [2049, 4096, 8192])
def test_log_line_fails_closed_when_scan_cap_is_exceeded(length: int) -> None:
    # Given an oversized non-secret line, followed by a useful diagnostic line.
    text = "z" * 2048 + "\n" + "z" * length + "\nretry later"

    # When the log boundary scans it.
    safe = redact_log_text(text)

    # Then it replaces the entire line rather than truncating before scanning.
    assert safe == "z" * 1024 + "\n<redacted>\nretry later"
