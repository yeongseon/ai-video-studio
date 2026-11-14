"""Tests for BGM service input validation and failure paths."""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from creator_service.bgm_service import BgmService


@pytest.fixture
def svc():
    return BgmService()


class TestBgmVolumeValidation:
    """Ensure bgm_volume is clamped to [0.0, 1.0]."""

    def test_volume_clamped_high(self, svc, tmp_path):
        narration = tmp_path / "narration.mp3"
        narration.write_bytes(b"\x00" * 100)
        bgm = tmp_path / "bgm.mp3"
        bgm.write_bytes(b"\x00" * 100)
        out = tmp_path / "mixed.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            svc.mix_audio_with_bgm(str(narration), str(bgm), str(out), bgm_volume=5.0)
            # Check the filter_complex contains clamped volume (1.0)
            cmd = mock_run.call_args[0][0]
            filter_arg = [a for a in cmd if "volume=" in a][0]
            assert "volume=1.0" in filter_arg

    def test_volume_clamped_negative(self, svc, tmp_path):
        narration = tmp_path / "narration.mp3"
        narration.write_bytes(b"\x00" * 100)
        bgm = tmp_path / "bgm.mp3"
        bgm.write_bytes(b"\x00" * 100)
        out = tmp_path / "mixed.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            svc.mix_audio_with_bgm(str(narration), str(bgm), str(out), bgm_volume=-1.0)
            cmd = mock_run.call_args[0][0]
            filter_arg = [a for a in cmd if "volume=" in a][0]
            assert "volume=0.0" in filter_arg


class TestDurationValidation:
    """Ensure duration_seconds is clamped."""

    def test_duration_clamped_high(self, svc, tmp_path):
        out = tmp_path / "bgm.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            svc.generate_ambient_bgm(99999.0, str(out), mood="emotional")
            cmd = mock_run.call_args[0][0]
            # -t argument should be clamped to 600.0
            t_idx = cmd.index("-t")
            assert float(cmd[t_idx + 1]) == 600.0

    def test_duration_clamped_low(self, svc, tmp_path):
        out = tmp_path / "bgm.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            svc.generate_ambient_bgm(-5.0, str(out), mood="emotional")
            cmd = mock_run.call_args[0][0]
            t_idx = cmd.index("-t")
            assert float(cmd[t_idx + 1]) == 1.0


class TestBgmGenerationFailure:
    """Test failure paths."""

    def test_ffmpeg_failure_raises_runtime_error(self, svc, tmp_path):
        out = tmp_path / "bgm.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="ffmpeg error")
            with pytest.raises(RuntimeError, match="BGM generation failed"):
                svc.generate_ambient_bgm(30.0, str(out))

    def test_mix_failure_raises_runtime_error(self, svc, tmp_path):
        narration = tmp_path / "narration.mp3"
        narration.write_bytes(b"\x00" * 100)
        bgm = tmp_path / "bgm.mp3"
        bgm.write_bytes(b"\x00" * 100)
        out = tmp_path / "mixed.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="mix error")
            with pytest.raises(RuntimeError, match="Audio mixing failed"):
                svc.mix_audio_with_bgm(str(narration), str(bgm), str(out))


class TestNoShellInjection:
    """Verify subprocess is called without shell=True."""

    def test_generate_uses_list_args(self, svc, tmp_path):
        out = tmp_path / "bgm.mp3"

        with patch("creator_service.bgm_service._run_ffmpeg") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            svc.generate_ambient_bgm(30.0, str(out))
            # First arg should be a list, not a string
            assert isinstance(mock_run.call_args[0][0], list)
            # shell should not be True
            assert mock_run.call_args[1].get("shell") is not True
