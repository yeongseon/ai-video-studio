import pickle
from pathlib import Path

import pytest
from creator_domain.exceptions import ServiceError
from creator_service.actionable_errors import build_run_failure_summary, failure_summary_from_code, map_service_error
from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import RenderProfile
from PIL import Image


def test_limit_error_roundtrips_and_maps_to_static_recoverable_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an actual failed delivery with a private output label.
    monkeypatch.setenv("RENDER_SCRATCH_MAX_BYTES", "1")
    image = tmp_path / "private.png"
    Image.new("RGB", (64, 64), "white").save(image)
    with pytest.raises(ServiceError) as raised:
        FFmpegService(RenderProfile(width=64, height=64)).render(
            RenderInput([image], None, None, [1]), tmp_path / "private-output.mp4",
        )
    # When crossing worker serialization and the error mapper.
    error = pickle.loads(pickle.dumps(raised.value))
    mapped = map_service_error(error)
    summary = build_run_failure_summary(error)
    # Then only static recovery data reaches both persistence/read surfaces.
    assert mapped.code == mapped.category.value == "RENDER_SCRATCH_LIMIT"
    assert mapped.retryable is True
    assert error.cap_bytes == 1 and error.observed_bytes > 1
    assert failure_summary_from_code(mapped.code) == summary
    assert str(tmp_path) not in str(summary)
    error.label = "token=synthetic-secret /private/path"
    assert build_run_failure_summary(error) == summary
    assert "RENDER_SCRATCH_MAX_BYTES" in " ".join(mapped.recovery_steps)
