from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def sweep_at_start() -> None:
    from creator_service.render_scratch import configured_scratch_root
    from creator_service.render_scratch_sweeper import SweepPolicy, sweep_render_scratch
    from tasks.render_video import render_video

    if os.getenv("RENDER_SCRATCH_SWEEP_ENABLED", "true").lower() != "true":
        return
    try:
        scratch = configured_scratch_root()
    except (OSError, ValueError):
        logger.warning("Render scratch sweep configuration invalid; skipping configured root")
        scratch = None
    policy = SweepPolicy(
        scratch_root=scratch,
        minimum_max_age_seconds=2 * render_video.time_limit,
        max_age_seconds=max(0, int(os.getenv("RENDER_SCRATCH_MAX_AGE_SECONDS", "3600"))),
        min_grace_seconds=max(0, int(os.getenv("RENDER_SCRATCH_SWEEP_MIN_GRACE_SECONDS", "60"))),
        max_entries=max(0, int(os.getenv("RENDER_SCRATCH_SWEEP_MAX_ENTRIES", "10000"))),
        max_removals=max(0, int(os.getenv("RENDER_SCRATCH_SWEEP_MAX_REMOVALS", "500"))),
        max_run_directories=max(0, int(os.getenv("RENDER_SCRATCH_SWEEP_MAX_RUN_DIRECTORIES", "1000"))),
        budget_seconds=max(0, int(os.getenv("RENDER_SCRATCH_SWEEP_BUDGET_SECONDS", "20"))),
    )
    sweep_render_scratch(Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")), policy)
