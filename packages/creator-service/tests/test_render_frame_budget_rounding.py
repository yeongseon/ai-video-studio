from __future__ import annotations

import random
from fractions import Fraction

import pytest

from creator_service.render_frame_budget import allocate_frames


def test_renderer_allocates_exact_half_frame_total_to_even() -> None:
    # Given the planner regression durations represented at the renderer boundary.
    durations = [float(Fraction(1, 3)), float(Fraction(5, 16))]

    # When the renderer allocates frames, then it shares exact ties-to-even behavior.
    assert sum(allocate_frames(durations, 24)) == 16


@pytest.mark.parametrize("fps", [24, 25, 30, 60])
@pytest.mark.parametrize("minutes", [10, 15, 20])
def test_renderer_and_exact_cumulative_budget_agree_for_long_form_inputs(
    fps: int,
    minutes: int,
) -> None:
    # Given deterministic long-form durations on half-frame boundaries.
    rng = random.Random(fps * 10_000 + minutes)
    total = Fraction(minutes * 60)
    durations: list[Fraction] = []
    remaining = total
    while remaining:
        duration = min(remaining, Fraction(rng.randint(fps * 3, fps * 17) * 2 + 1, fps * 2))
        durations.append(duration)
        remaining -= duration

    # When renderer budgets are allocated, then their total equals exact rational rounding.
    budgets = allocate_frames([float(duration) for duration in durations], fps)

    assert sum(budgets) == round(total * fps)
