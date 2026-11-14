import hashlib
import importlib
import socket
from pathlib import Path

import pytest


@pytest.mark.parametrize("configured", [False, True])
def test_timeout_removes_only_killed_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool,
) -> None:
    # Given real scratch, published content, and competing deliveries in both layouts.
    task = importlib.import_module("tasks.render_video")
    root = tmp_path / "artifacts"
    render = root / "1/render"
    render.mkdir(parents=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.setattr(task, "_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(root))
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    host = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:12]
    run = hashlib.sha256(str(render.resolve()).encode()).hexdigest()[:12]
    owned = []
    kept = [render / "timeline-abcdefgh", render / ".render_tmp_43_deadbeef"]
    if configured:
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
        for parent in (render, scratch):
            for prefix in (".render_tmp_", ".render_plan_", "timeline-", "sfx_"):
                owned.append(parent / f"{prefix}{host}_{run}_42_abcdefgh")
                kept.extend([
                    parent / f"{prefix}{host}_{run}_43_abcdefgh",
                    parent / f"{prefix}{'f' * 12}_{run}_42_abcdefgh",
                    parent / f"{prefix}{host}_{'e' * 12}_42_abcdefgh",
                ])
    else:
        owned.extend(render / f"{prefix}42_deadbeef" for prefix in (".render_tmp_", ".render_plan_"))
    other_run = root / "2/render/.render_tmp_42_deadbeef"
    kept.append(other_run)
    for path in (*owned, *kept):
        path.mkdir(parents=True)
        (path / "payload").write_bytes(b"keep unless owned")
    partial = render / "output.tmp.42.deadbeef.mp4"
    partial.write_bytes(b"partial")
    other_partial = render / "output.tmp.43.deadbeef.mp4"
    other_partial.write_bytes(b"other delivery")
    published = render / "output.mp4"
    published.write_bytes(b"published")
    unrelated = render / "audio.wav"
    unrelated.write_bytes(b"unrelated")
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "payload").write_bytes(b"victim")
    link = render / ".render_plan_42_12345678"
    link.symlink_to(victim, target_is_directory=True)
    # When the real task's registered hard-timeout callable runs, twice.
    for _ in range(2):
        task.render_video.hard_timeout_cleanup(1, "delivery", "celery@custom-node", 42)
    # Then only attributable direct children disappear; no symlink is followed or removed.
    assert not any(path.exists() for path in (*owned, partial))
    assert all((path / "payload").read_bytes() == b"keep unless owned" for path in kept)
    assert published.read_bytes() == b"published"
    assert unrelated.read_bytes() == b"unrelated"
    assert other_partial.read_bytes() == b"other delivery"
    assert link.is_symlink()
    assert (victim / "payload").read_bytes() == b"victim"


def test_timeout_tolerates_missing_directories_and_invalid_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given absent artifacts and an invalid scratch setting.
    task = importlib.import_module("tasks.render_video")
    monkeypatch.setattr(task, "_ARTIFACT_ROOT", str(tmp_path / "missing"))
    monkeypatch.setenv("RENDER_SCRATCH_DIR", "relative")
    # When the main-process callback runs; then neither case escapes the handler.
    assert task.render_video.hard_timeout_cleanup(1, "delivery", "worker", 42) is None


def test_timeout_contains_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a failure at the filesystem boundary.
    task = importlib.import_module("tasks.render_video")

    def broken() -> Path:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(task, "configured_scratch_root", broken)
    # When the callback runs; then it logs rather than raising into Celery.
    task.render_video.hard_timeout_cleanup(1, "delivery", "worker", 42)
    assert "Render timeout cleanup failed" in caplog.text


def test_timeout_reports_entry_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    # Given more attributable entries than the main-process enumeration budget.
    task = importlib.import_module("tasks.render_video")
    cleanup = importlib.import_module("creator_service.render_scratch_cleanup")
    render = tmp_path / "1/render"
    render.mkdir(parents=True)
    for suffix in ("deadbeef", "12345678"):
        (render / f".render_tmp_42_{suffix}").mkdir()
    monkeypatch.setattr(task, "_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    monkeypatch.setattr(cleanup, "TIMEOUT_MAX_ENTRIES", 1)
    # When cleanup is bounded to one examined entry.
    with caplog.at_level("INFO"):
        task.render_video.hard_timeout_cleanup(1, "delivery", "worker", 42)
    # Then it leaves the overflow for a later sweep and reports truncation.
    assert len(list(render.iterdir())) == 1
    assert "Render timeout cleanup entry bound reached" in caplog.text


@pytest.mark.parametrize("level", ["run", "render"])
def test_timeout_refuses_symlinked_run_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str,
) -> None:
    # Given a redirected run or render directory containing otherwise attributable scratch.
    task = importlib.import_module("tasks.render_video")
    root = tmp_path / "artifacts"
    root.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    if level == "run":
        (root / "1").symlink_to(victim, target_is_directory=True)
        parent = victim / "render"
        parent.mkdir()
    else:
        (root / "1").mkdir()
        (root / "1/render").symlink_to(victim, target_is_directory=True)
        parent = victim
    entry = parent / ".render_tmp_42_deadbeef"
    entry.mkdir()
    monkeypatch.setattr(task, "_ARTIFACT_ROOT", str(root))
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    # When timeout cleanup runs; then the redirected contents survive.
    task.render_video.hard_timeout_cleanup(1, "delivery", "worker", 42)
    assert entry.is_dir()
