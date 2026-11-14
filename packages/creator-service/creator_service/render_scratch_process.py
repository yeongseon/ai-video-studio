"""Conservative Linux process identity for scratch reclamation."""

from __future__ import annotations

import os
import time
from pathlib import Path


def process_start_time(
    pid: int, *, proc_root: Path = Path("/proc"), clock_ticks: int | None = None,
) -> float | None:
    """Return epoch seconds, or unknown when procfs cannot prove a plausible start."""
    if not 1 <= pid <= 4194304:
        return None
    try:
        frequency = os.sysconf("SC_CLK_TCK") if clock_ticks is None else clock_ticks
        if frequency <= 0:
            return None
        # comm is parenthesised but may itself contain spaces and closing parentheses.
        fields = (proc_root / str(pid) / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        ticks = int(fields[19])
        if ticks < 0:
            return None
        for line in (proc_root / "stat").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and parts[0] == "btime":
                _, raw_boot = parts
                boot = int(raw_boot)
                started = boot + ticks / frequency
                if boot <= 0 or not 86400 <= started <= time.time() + 86400:
                    return None
                return started
    except (OSError, ValueError, IndexError, UnicodeDecodeError, AttributeError, OverflowError):
        return None
    return None
