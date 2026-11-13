"""Beat's liveness evidence: the last time it successfully PUBLISHED its heartbeat.

Celery advances a schedule entry's timestamp before publishing and only logs a failed publish,
so those timestamps cannot prove dispatch. The receipt is written by the Beat process once the
broker has accepted the message, and does not depend on a worker being free: the local worker
is a solo pool, so any long task would otherwise make a healthy Beat look dead.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError


HEARTBEAT_INTERVAL_SECONDS: Final = 60.0
HEARTBEAT_MAX_AGE_SECONDS: Final = HEARTBEAT_INTERVAL_SECONDS * 3


class Heartbeat(BaseModel):
    model_config = ConfigDict(frozen=True)
    last_run_at: AwareDatetime


def heartbeat_path() -> Path:
    return Path(os.getenv("BEAT_HEARTBEAT_PATH", "/var/lib/short-form-studio/dlq/beat-heartbeat.json"))


def record_heartbeat(path: Path, now: datetime) -> None:
    """Replace the receipt atomically, so a reader never sees a partial write."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(Heartbeat(last_run_at=now).model_dump_json(), encoding="utf-8")
    temporary.replace(path)


def is_healthy(path: Path, now: datetime) -> bool:
    """Fail closed until Beat has recently published its heartbeat."""
    try:
        receipt = Heartbeat.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return False
    age = (now - receipt.last_run_at).total_seconds()
    return 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS


def main() -> int:
    healthy = is_healthy(heartbeat_path(), datetime.now(timezone.utc))
    print("healthy" if healthy else "unhealthy")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
