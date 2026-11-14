import os
from pathlib import Path

import pytest
from celery.signals import worker_ready


def test_ready_reclaims_reused_pid_after_same_container_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given same-host configured scratch from pid 7 and a published sibling.
    import hashlib
    import socket

    import celery_app
    import creator_service.render_scratch_sweeper as module

    root = tmp_path / "artifacts"
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
    crashed = scratch / f"timeline-{host}_{'a' * 12}_7_abcdefgh"
    sibling = scratch / f"timeline-{host}_{'a' * 12}_8_abcdefgh"
    for entry in (crashed, sibling):
        entry.mkdir()
        (entry / "payload").write_bytes(b"scratch")
        os.utime(entry, (1, 1))
    output = root / "1/render/output.mp4"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"published")
    changed = max(entry.stat().st_ctime for entry in (crashed, sibling))
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    monkeypatch.setenv("RENDER_SCRATCH_MAX_AGE_SECONDS", "0")
    monkeypatch.setenv("RENDER_SCRATCH_SWEEP_ENABLED", "true")
    assert "pid_start_time" in module.SweepRuntime.__dataclass_fields__, "startup needs process identity"
    original = module.sweep_render_scratch

    def restart(root: Path, policy: module.SweepPolicy) -> module.SweepResult:
        return original(root, policy, module.SweepRuntime(
            now=lambda: changed + 120, pid_alive=lambda pid: True,
            pid_start_time=lambda pid: changed + 10 if pid == 7 else changed - 10,
        ))

    monkeypatch.setattr(module, "sweep_render_scratch", restart)
    # When the next Worker emits its real ready signal.
    responses = worker_ready.send(sender=None)
    # Then only the reused owner's scratch is removed by startup cleanup.
    assert (celery_app.cleanup_render_scratch_at_start, None) in responses
    assert not crashed.exists()
    assert (sibling / "payload").read_bytes() == b"scratch"
    assert output.read_bytes() == b"published"
