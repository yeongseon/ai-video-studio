"""Real cumulative-frame allocation across render paths."""
import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from creator_domain.models import EncodingProfile, OutputSpec, RenderPlan, RenderSegment, RenderSegmentKind
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import RenderProfile, TransitionStyle
from creator_service.render_segments import render_plan


@pytest.mark.parametrize("fps", [15, 24, 25, 30, 60])
@pytest.mark.parametrize("duration", [0.1, 0.2, 1.0, 1 / 6, 2.4])
@pytest.mark.parametrize("count", [1, 5, 30])
@pytest.mark.parametrize("kind", ["image", "mixed", "ken_burns"])
def test_render_preserves_total_frame_budget(tmp_path: Path, fps: int, duration: float,
                                            count: int, kind: str) -> None:
    # Given: cumulative placement boundaries, including fractional source frames.
    image = tmp_path / "still.png"
    Image.new("RGB", (64, 64), "red").save(image)
    profile = RenderProfile(width=64, height=64, fps=fps, preset="ultrafast",
                            transition_style=TransitionStyle.KEN_BURNS if kind == "ken_burns" else TransitionStyle.CUT)
    total = duration * count
    if kind == "ken_burns":
        # When
        output = FFmpegService(profile).render(RenderInput([image] * count, None, None, [duration] * count),
                                              tmp_path / "out.mp4")
    else:
        video = tmp_path / "video.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
            f"testsrc2=size=64x64:rate={fps}:duration=3", "-c:v", "libx264", "-threads", "1",
            "-preset", "ultrafast", str(video)], check=True, capture_output=True, timeout=30)
        segments = [RenderSegment(kind=RenderSegmentKind.VIDEO if kind == "mixed" and i % 2 else RenderSegmentKind.IMAGE,
            source=str(video if kind == "mixed" and i % 2 else image),
            timeline_start_seconds=i * duration, duration_seconds=duration) for i in range(count)]
        plan = RenderPlan(segments=segments, output_spec=OutputSpec(width=64, height=64, fps=fps),
                          encoding_profile=EncodingProfile.from_render_profile(profile))
        # When
        output = render_plan(plan, tmp_path / "out.mp4")
    # Then: metadata sample count, not the nominal FPS label, matches the plan exactly.
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=nb_frames:format=duration",
        "-of", "json", str(output)], check=True, capture_output=True, text=True, timeout=15)
    doc = json.loads(probe.stdout)
    assert int(doc["streams"][0]["nb_frames"]) == round(total * fps)
    assert abs(float(doc["format"]["duration"]) - round(total * fps) / fps) <= 0.002
