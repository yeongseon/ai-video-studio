from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from creator_domain.exceptions import ServiceError
from creator_domain.models import RenderPlan
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.media_integrity import MediaExpectation, validate_media
from creator_service.render_profile import RenderProfile
from creator_service.render_segments import render_plan


@pytest.mark.parametrize("renderer", ["legacy", "plan"])
@pytest.mark.parametrize("configured", [False, True])
def test_real_ffmpeg_limit_preserves_output_and_cleans_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, renderer: str, configured: bool,
) -> None:
    # Given real synthetic input, a published output and another delivery's scratch.
    monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    monkeypatch.delenv("RENDER_SCRATCH_DIR", raising=False)
    if configured:
        monkeypatch.setenv("RENDER_SCRATCH_DIR", str(scratch))
    other = scratch / "other-delivery"
    other.mkdir()
    (other / "keep").write_bytes(b"untouched")
    image = tmp_path / "image.png"
    Image.effect_noise((320, 320), 100).convert("RGB").save(image)
    output = tmp_path / "artifacts" / "output.mp4"
    output.parent.mkdir()
    output.write_bytes(b"pre-existing published video")
    digest = hashlib.sha256(output.read_bytes()).digest()
    processes = []
    popen = subprocess.Popen

    def observe(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", observe)
    profile = RenderProfile(width=320, height=320, preset="ultrafast", crf=18)
    plan = RenderPlan.model_validate({
        "segments": [{"kind": "image", "source": str(image), "duration_seconds": 2,
                      "timeline_start_seconds": 0}],
        "output_spec": {"width": 320, "height": 320, "fps": 30},
        "encoding_profile": {"name": "test", "preset": "ultrafast", "crf": 18},
    })

    def render() -> Path:
        if renderer == "plan":
            return render_plan(plan, output)
        return FFmpegService(profile).render(RenderInput([image], None, None, [2]), output)

    # When a tiny cap fails and a generous cap succeeds, then publication stays atomic.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1")
    with pytest.raises(ServiceError) as raised:
        render()
    assert type(raised.value).__name__ == "RenderScratchLimitExceeded"
    assert processes
    for process in processes:
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    assert hashlib.sha256(output.read_bytes()).digest() == digest
    assert list(output.parent.iterdir()) == [output]
    assert list(scratch.iterdir()) == [other]
    assert (other / "keep").read_bytes() == b"untouched"
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "2147483648")
    assert render() == output
    validate_media(output, MediaExpectation(profile, 2, False))
    assert list(output.parent.iterdir()) == [output]
    assert list(scratch.iterdir()) == [other]
