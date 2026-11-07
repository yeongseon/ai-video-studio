"""Cumulative CFR boundaries prevent per-segment rounding accumulation."""
from creator_domain.models.frame_budget import cumulative_frame_ends


def allocate_frames(durations: list[float], fps: int) -> list[int]:
    ends = cumulative_frame_ends(durations, fps)
    starts = [0, *ends[:-1]]
    budgets = [end - start for start, end in zip(starts, ends, strict=True)]
    if any(budget < 1 for budget in budgets):
        raise ValueError("Each segment must occupy at least one output frame")
    return budgets
