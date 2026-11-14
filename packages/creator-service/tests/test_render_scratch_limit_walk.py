from __future__ import annotations

import errno
import logging
import os
import sys
from pathlib import Path

import pytest
from creator_domain.exceptions import ServiceError
from creator_service import ffmpeg_service
from creator_service.ffmpeg_service import FFmpegService, RenderInput, _run_ffmpeg
from creator_service.render_scratch_monitor import RenderScratchBudget


class Completed(Exception):
    pass


class NestedWriter(FFmpegService):
    def _render_segment(self, image_path, duration, output_path, transition_override=None):
        script = """
import sys,time
from pathlib import Path
root = Path(sys.argv[1]).parent
for i in range(8):
    folder = root / str(i)
    folder.mkdir()
    (folder / 'data').write_bytes(b'x'*256)
time.sleep(0.5)
"""
        _run_ffmpeg([sys.executable, "-c", script, str(output_path)], timeout=5)
        raise Completed()


def test_bounded_walk_resumes_instead_of_forgetting_partial_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a directory tree requiring many small slices of the accounting walk.
    monitor = sys.modules.get("creator_service.render_scratch_monitor")
    if monitor is not None:
        monkeypatch.setattr(monitor, "MAX_ENTRIES_PER_POLL", 2)
    monkeypatch.setattr(ffmpeg_service, "POLL_SECONDS", 0.01, raising=False)
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    # When each individual file is below the cap, then their aggregate still fails.
    with pytest.raises(ServiceError):
        NestedWriter().render(RenderInput([tmp_path / "input"], None, None, [1]), tmp_path / "output.mp4")


def test_walk_ignores_outside_symlinks_but_counts_owned_regular_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a private tree linked to a larger outside file and directory.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 4096)
    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "file-link").symlink_to(outside / "large")
    (owned / "dir-link").symlink_to(outside, target_is_directory=True)
    (owned / "small").write_bytes(b"x" * 1024)
    class LinkWriter(FFmpegService):
        def _render_segment(self, image_path, duration, output_path, transition_override=None):
            (output_path.parent / "link").symlink_to(outside, target_is_directory=True)
            (output_path.parent / "file-link").symlink_to(outside / "large")
            _run_ffmpeg([sys.executable, "-c", "import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x'*int(sys.argv[2])); time.sleep(0.6)",
                         str(output_path), str(int(duration))], timeout=5)
            raise Completed()

    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    # When measured then an outside link is excluded, but a real excess fails.
    with pytest.raises(Completed):
        LinkWriter().render(RenderInput([owned], None, None, [1024]), tmp_path / "output.mp4")
    with pytest.raises(ServiceError):
        LinkWriter().render(RenderInput([owned], None, None, [1025]), tmp_path / "output.mp4")


@pytest.mark.parametrize("code", [errno.EACCES, errno.ELOOP, errno.EMFILE], ids=["eacces", "eloop", "emfile"])
def test_walk_skips_a_subtree_that_cannot_be_opened_instead_of_failing_the_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, code: int,
) -> None:
    # Given a scratch tree with a readable file and a subdirectory whose open fails.
    root = tmp_path / "scratch"
    (root / "locked").mkdir(parents=True)
    (root / "locked" / "hidden").write_bytes(b"x" * 1000)
    (root / "readable").write_bytes(b"x" * 10)
    real_open = os.open

    def flaky_open(path, flags, *args, **kwargs):
        if os.fspath(path) == "locked":
            raise OSError(code, "injected")
        return real_open(path, flags, *args, **kwargs)

    budget = RenderScratchBudget(cap_bytes=100, label="walk")
    budget.register(root)

    # When the budget scans it, with the open of that subdirectory failing.
    with caplog.at_level(logging.WARNING), monkeypatch.context() as patch:
        patch.setattr(os, "open", flaky_open)
        budget.check()

    # Then it counts what it can read, keeps the render alive and reports the errno once.
    assert budget._total == 10
    skipped = [record for record in caplog.records if "skipped" in record.message]
    assert len(skipped) == 1
    assert errno.errorcode[code] in skipped[0].getMessage()
