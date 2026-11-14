"""The three ffprobe call sites decode the probe's output leniently, so an unprobeable file ends as its intended error (#1106)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
from creator_service.ffmpeg_service import FFmpegService
from creator_service.media_asset_service import MediaUploadRejected, ProbeResult, probe_media_metadata
from creator_service.render_segments import SegmentSourceError, _probe_source_duration

NOT_UTF8 = b"caf\xe9 \xff\xfe tail\n"
VALID_UTF8 = "na\u00efve caf\u00e9 tail\n".encode()
PLACEHOLDER = b"placeholder bytes that are not media"
VIDEO_JSON = json.dumps(
    {
        "streams": [{"codec_type": "video", "width": 32, "height": 24, "duration": "1.0"}],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1.0"},
    }
).encode()


class ProbeSite(NamedTuple):
    probe: Callable[[Path], object]
    rejection: type[Exception]
    message: str
    success_stdout: bytes
    success_result: object


SITES = {
    "media-upload": ProbeSite(
        lambda path: probe_media_metadata(path, kind="video"),
        MediaUploadRejected,
        "not a recognized media file",
        VIDEO_JSON,
        ProbeResult(32, 24, 1.0, "video/mp4", len(PLACEHOLDER)),
    ),
    "segment-source": ProbeSite(
        _probe_source_duration,
        SegmentSourceError,
        "segment source is not valid media",
        VIDEO_JSON,
        1.0,
    ),
    "audio-duration": ProbeSite(
        lambda path: FFmpegService().get_audio_duration(str(path)),
        RuntimeError,
        "ffprobe failed for",
        b"1.0\n",
        1.0,
    ),
}
EVERY_SITE = pytest.mark.parametrize("site", SITES.values(), ids=SITES.keys())
STDERR = pytest.mark.parametrize("stderr", [NOT_UTF8, VALID_UTF8], ids=["not-utf8", "utf8"])


def shadow_ffprobe(
    directory: Path, monkeypatch: pytest.MonkeyPatch, *, stdout: bytes = b"", stderr: bytes, exit_code: int,
) -> None:
    """Put an ffprobe stand-in that writes the given raw bytes ahead of the real one on PATH."""
    script = (
        f"import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r}); sys.exit({exit_code})"
    )
    bin_dir = directory / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "ffprobe"
    executable.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(script)}\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@EVERY_SITE
@STDERR
def test_a_failed_probe_ends_as_the_intended_error_whatever_bytes_ffprobe_echoes(
    site: ProbeSite, stderr: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an ffprobe that cannot read the file and echoes the given bytes, valid UTF-8 or not, on stderr.
    media = tmp_path / "clip.bin"
    media.write_bytes(PLACEHOLDER)
    shadow_ffprobe(tmp_path, monkeypatch, stderr=stderr, exit_code=1)
    # When the file is probed.
    # Then the call site raises its intended error, not an unclassified decoding failure.
    with pytest.raises(site.rejection, match=site.message):
        site.probe(media)


@EVERY_SITE
@STDERR
def test_a_successful_probe_is_not_failed_by_the_bytes_on_its_stderr(
    site: ProbeSite, stderr: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an ffprobe that succeeds but also echoes the given bytes on stderr.
    media = tmp_path / "clip.bin"
    media.write_bytes(PLACEHOLDER)
    shadow_ffprobe(tmp_path, monkeypatch, stdout=site.success_stdout, stderr=stderr, exit_code=0)
    # When the file is probed.
    result = site.probe(media)
    # Then the usual result is returned.
    assert result == site.success_result


def test_the_audio_duration_error_keeps_the_readable_part_of_a_stderr_that_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an ffprobe that fails with stderr bytes that are not valid UTF-8.
    media = tmp_path / "clip.bin"
    media.write_bytes(PLACEHOLDER)
    shadow_ffprobe(tmp_path, monkeypatch, stderr=NOT_UTF8, exit_code=1)
    # When the duration is requested.
    with pytest.raises(RuntimeError) as raised:
        FFmpegService().get_audio_duration(str(media))
    # Then the error text shows the readable parts around replacement characters.
    message = str(raised.value)
    assert "caf\ufffd" in message
    assert message.rstrip().endswith("tail")


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="the real ffprobe binary is required")
@EVERY_SITE
def test_real_ffprobe_on_an_unreadable_file_with_a_name_that_is_not_utf8_ends_as_the_intended_error(
    site: ProbeSite, tmp_path: Path,
) -> None:
    # Given a file that is not media, whose name holds a byte that is not valid UTF-8, so the real ffprobe echoes the name on stderr.
    unreadable = tmp_path / os.fsdecode(b"clip-\xff.bin")
    unreadable.write_bytes(PLACEHOLDER)
    # When it is probed with the real ffprobe.
    # Then the call site raises its intended error.
    with pytest.raises(site.rejection, match=site.message):
        site.probe(unreadable)
