"""Cumulative CFR boundaries prevent per-segment rounding accumulation."""
import math


def allocate_frames(durations: list[float], fps: int) -> list[int]:
    ends = [round(math.fsum(durations[:index + 1]) * fps) for index in range(len(durations))]
    starts = [0, *ends[:-1]]
    budgets = [end - start for start, end in zip(starts, ends, strict=True)]
    if any(budget < 1 for budget in budgets):
        raise ValueError("Each segment must occupy at least one output frame")
    return budgets
