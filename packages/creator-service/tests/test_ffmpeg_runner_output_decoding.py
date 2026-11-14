"""The shared FFmpeg runner decodes its child's output leniently and never lets its drain mask a failure (#1103)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from creator_service.ffmpeg_service import _run_ffmpeg
from creator_service.render_scratch_monitor import RenderScratchLimitExceeded, render_budget

NOT_UTF8 = b"caf\xe9 \xff\xfe tail"


@pytest.mark.parametrize(("stream", "exit_code"), [("stderr", 0), ("stdout", 3)], ids=["stderr-success", "stdout-failure"])
def test_output_that_is_not_utf8_is_decoded_with_replacement_characters(stream: str, exit_code: int) -> None:
    # Given a child that echoes bytes which are not valid UTF-8, such as a legacy-encoded metadata tag.
    script = f"import sys; sys.{stream}.buffer.write({NOT_UTF8!r}); sys.{stream}.flush(); sys.exit({exit_code})"
    # When the runner collects the output.
    result = _run_ffmpeg([sys.executable, "-c", script], timeout=10)
    # Then the exit status is reported and the text keeps its readable parts around replacement characters.
    assert result.returncode == exit_code
    text = result.stderr if stream == "stderr" else result.stdout
    assert text.startswith("caf")
    assert "\ufffd" in text
    assert text.endswith("tail")


def test_valid_utf8_output_is_returned_unchanged() -> None:
    # Given a child that writes valid non-ASCII text to both streams.
    script = "import sys; sys.stdout.write('caf\\u00e9 out\\n'); sys.stderr.write('na\\u00efve err\\n')"
    # When the runner collects the output.
    result = _run_ffmpeg([sys.executable, "-c", script], timeout=10)
    # Then the text is exactly what the child wrote.
    assert (result.returncode, result.stdout, result.stderr) == (0, "café out\n", "naïve err\n")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="the real FFmpeg binary is required")
def test_real_ffmpeg_pass_over_a_source_tag_that_is_not_utf8_succeeds(tmp_path: Path) -> None:
    # Given a real video whose title tag holds bytes that are not valid UTF-8, written by the muxer as they are.
    source = tmp_path / "tagged.mp4"
    subprocess.run(
        [b"ffmpeg", b"-y", b"-nostdin", b"-v", b"error", b"-f", b"lavfi", b"-i", b"color=c=black:s=64x64:d=0.2",
         b"-metadata", b"title=" + NOT_UTF8, os.fsencode(source)],
        check=True,
    )
    # When the runner executes a pass over it with FFmpeg's default log level, which echoes the input metadata.
    result = _run_ffmpeg(["ffmpeg", "-y", "-nostdin", "-i", str(source), "-f", "null", "-"], timeout=60)
    # Then FFmpeg's success is reported instead of a decoding failure.
    assert result.returncode == 0


@pytest.mark.parametrize(
    ("injected", "propagated"),
    [(ValueError, RenderScratchLimitExceeded), (KeyboardInterrupt, KeyboardInterrupt)],
    ids=["failure-is-contained", "interrupt-still-propagates"],
)
def test_a_failing_final_drain_cannot_replace_a_scratch_limit_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, injected: type[BaseException], propagated: type[BaseException],
) -> None:
    # Given a real child that outgrows its allowance and a final drain that raises.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1024")
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[str]] = []

    def launch(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        real_wait, real_communicate = proc.wait, proc.communicate
        draining = False

        def wait(*wait_args, **wait_kwargs):
            nonlocal draining
            draining = True
            return real_wait(*wait_args, **wait_kwargs)

        def communicate(*communicate_args, **communicate_kwargs):
            if draining:
                raise injected("injected drain failure")
            return real_communicate(*communicate_args, **communicate_kwargs)

        proc.wait = wait
        proc.communicate = communicate
        return proc

    monkeypatch.setattr(subprocess, "Popen", launch)
    script = "import sys, time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'x' * 4096); time.sleep(30)"
    with render_budget("drain") as budget:
        budget.register(tmp_path)
        # When the breach is detected and the final drain fails, then an ordinary failure never replaces the breach.
        with pytest.raises(propagated):
            _run_ffmpeg([sys.executable, "-c", script, str(tmp_path / "data")], timeout=20)
    # Then the child was reaped and both pipes are closed.
    assert processes[0].returncode is not None
    assert processes[0].stdout is not None and processes[0].stdout.closed
    assert processes[0].stderr is not None and processes[0].stderr.closed
