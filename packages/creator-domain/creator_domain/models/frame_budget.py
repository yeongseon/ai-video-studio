from __future__ import annotations

from fractions import Fraction
from typing import Final

from creator_domain.exceptions import ChunkManifestValidationError

_SOURCE_TIME_MAX_DENOMINATOR: Final = 1_000_000_000


def source_time_fraction(value: float) -> Fraction:
    """Recover a deterministic source-time rational from its binary float boundary value."""
    return Fraction.from_float(value).limit_denominator(_SOURCE_TIME_MAX_DENOMINATOR)


def cumulative_frame_ends(durations: list[float], fps: int) -> tuple[int, ...]:
    elapsed = Fraction()
    ends: list[int] = []
    for duration in durations:
        elapsed += source_time_fraction(duration)
        ends.append(round(elapsed * fps))
    return tuple(ends)


def require_contiguous_placements(
    placements: list[tuple[float, float]],
    fps: int,
) -> None:
    expected_start_frame = 0
    for start, duration in placements:
        start_time = source_time_fraction(start)
        actual_start_frame = round(start_time * fps)
        if actual_start_frame != expected_start_frame:
            raise ChunkManifestValidationError(
                "render segment placements must be contiguous on the canonical frame grid"
            )
        expected_start_frame = round((start_time + source_time_fraction(duration)) * fps)
