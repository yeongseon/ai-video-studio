from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _integer(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value > 0 or (allow_zero and value == 0):
        return value
    logger.warning("Invalid %s; using default", name)
    return default


def sweep_at_start() -> None:
    from creator_service.render_scratch import configured_scratch_root
    from creator_service.render_scratch_sweeper import SweepPolicy, sweep_render_scratch
    from tasks.render_video import render_video

    enabled = os.getenv("RENDER_SCRATCH_SWEEP_ENABLED", "true").lower()
    if enabled not in {"1", "true", "yes", "0", "false", "no"}:
        logger.warning("Invalid RENDER_SCRATCH_SWEEP_ENABLED; using default")
        enabled = "true"
    if enabled in {"0", "false", "no"}:
        return
    try:
        scratch = configured_scratch_root()
    except (OSError, ValueError):
        logger.warning("Render scratch sweep configuration invalid; skipping configured root")
        scratch = None
    policy = SweepPolicy(
        scratch_root=scratch,
        minimum_max_age_seconds=2 * render_video.time_limit,
        max_age_seconds=_integer("RENDER_SCRATCH_MAX_AGE_SECONDS", 3600, allow_zero=True),
        min_grace_seconds=_integer("RENDER_SCRATCH_SWEEP_MIN_GRACE_SECONDS", 60),
        max_entries=_integer("RENDER_SCRATCH_SWEEP_MAX_ENTRIES", 10000),
        max_removals=_integer("RENDER_SCRATCH_SWEEP_MAX_REMOVALS", 500),
        max_run_directories=_integer("RENDER_SCRATCH_SWEEP_MAX_RUN_DIRECTORIES", 1000),
        budget_seconds=_integer("RENDER_SCRATCH_SWEEP_BUDGET_SECONDS", 20),
    )
    sweep_render_scratch(Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")), policy)
