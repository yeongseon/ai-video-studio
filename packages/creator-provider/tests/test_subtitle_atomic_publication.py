import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["whisper_provider", "groq_stt_provider"])
@pytest.mark.parametrize("fmt", ["srt", "vtt"])
def test_subtitle_writer_replaces_without_mutating_published_inode(tmp_path: Path, module: str, fmt: str) -> None:
    # Given an existing subtitle still held by an authorized download.
    from importlib import import_module

    provider = import_module(f"creator_provider.stt.{module}")
    target = tmp_path / f"subtitles.{fmt}"
    target.write_text("old generation")
    with target.open() as old:
        inode = os.fstat(old.fileno()).st_ino
        # When the shared subtitle publication seam writes a new generation.
        provider.publish_text(target, "new generation")
        # Then old readers stay immutable and the path points at a different inode.
        assert old.read() == "old generation"
        assert target.read_text() == "new generation"
        assert target.stat().st_ino != inode
