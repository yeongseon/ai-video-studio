import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from creator_service.ffmpeg_service import FFmpegService, RenderInput
from creator_service.render_profile import AudioCodec, Codec, RenderProfile, TransitionStyle


class TestRenderProfile(unittest.TestCase):
    """Tests for RenderProfile dataclass."""

    def test_default_profile(self):
        """Test default profile values."""
        profile = RenderProfile.default()
        
        self.assertEqual(profile.name, "shorts_default")
        self.assertEqual(profile.width, 1080)
        self.assertEqual(profile.height, 1920)
        self.assertEqual(profile.fps, 30)
        self.assertEqual(profile.video_codec, Codec.H264)
        self.assertEqual(profile.audio_codec, AudioCodec.AAC)
        self.assertEqual(profile.transition_style, TransitionStyle.FADE)
        self.assertEqual(profile.crf, 18)
        self.assertEqual(profile.preset, "fast")
        self.assertTrue(profile.burn_subtitles)
        self.assertEqual(profile.subtitle_font_size, 48)

    def test_high_quality_preset(self):
        """Test high_quality preset."""
        profile = RenderProfile.high_quality()
        
        self.assertEqual(profile.name, "high_quality")
        self.assertEqual(profile.crf, 18)
        self.assertEqual(profile.preset, "slow")
        # Check that inherited values are still correct
        self.assertEqual(profile.width, 1080)
        self.assertEqual(profile.height, 1920)

    def test_fast_preview_preset(self):
        """Test fast_preview preset."""
        profile = RenderProfile.fast_preview()
        
        self.assertEqual(profile.name, "fast_preview")
        self.assertEqual(profile.width, 540)
        self.assertEqual(profile.height, 960)
        self.assertEqual(profile.fps, 15)
        self.assertEqual(profile.crf, 28)
        self.assertEqual(profile.preset, "ultrafast")

    def test_codec_enum_values(self):
        """Test video codec enum values."""
        self.assertEqual(Codec.H264.value, "libx264")
        self.assertEqual(Codec.H265.value, "libx265")

    def test_audio_codec_enum_values(self):
        """Test audio codec enum values."""
        self.assertEqual(AudioCodec.AAC.value, "aac")
        self.assertEqual(AudioCodec.MP3.value, "libmp3lame")

    def test_transition_style_enum_values(self):
        """Test transition style enum values."""
        self.assertEqual(TransitionStyle.CUT.value, "cut")
        self.assertEqual(TransitionStyle.FADE.value, "fade")
        self.assertEqual(TransitionStyle.KEN_BURNS.value, "ken_burns")


class TestFFmpegService(unittest.TestCase):
    """Tests for FFmpegService."""

    def setUp(self):
        """Set up test fixtures."""
        self.profile = RenderProfile.default()
        self.service = FFmpegService(self.profile)

    def test_build_command_basic(self):
        """Test build_command produces valid FFmpeg command list."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png"), Path("/tmp/img2.png")],
            audio_path=None,
            subtitle_path=None,
            scene_durations=[3.0, 4.0]
        )
        output_path = Path("/tmp/output.mp4")
        
        cmd = self.service.build_command(input_data, output_path)
        
        # Verify it's a list of strings
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(item, str) for item in cmd))
        
        # Verify key elements are present
        self.assertIn("ffmpeg", cmd)
        self.assertIn("-y", cmd)  # overwrite flag
        self.assertIn(str(output_path), cmd)
        
        # Verify codec settings
        self.assertIn("-c:v", cmd)
        self.assertIn(self.profile.video_codec.value, cmd)
        self.assertIn("-crf", cmd)
        self.assertIn(str(self.profile.crf), cmd)
        self.assertIn("-preset", cmd)
        self.assertIn(self.profile.preset, cmd)

    def test_build_command_with_audio(self):
        """Test build_command includes audio mapping."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=Path("/tmp/audio.mp3"),
            subtitle_path=None,
            scene_durations=[5.0]
        )
        output_path = Path("/tmp/output.mp4")
        
        cmd = self.service.build_command(input_data, output_path)
        
        # Verify audio codec is set
        self.assertIn("-c:a", cmd)
        self.assertIn(self.profile.audio_codec.value, cmd)

    def test_build_command_with_subtitles(self):
        """Test build_command includes subtitle filter inside filter_complex."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=None,
            subtitle_path=Path("/tmp/subs.srt"),
            scene_durations=[5.0]
        )
        output_path = Path("/tmp/output.mp4")

        cmd = self.service.build_command(input_data, output_path)

        # Subtitle filter must be inside -filter_complex, NOT a separate -vf
        self.assertNotIn("-vf", cmd)
        fc_idx = cmd.index("-filter_complex")
        fc_value = cmd[fc_idx + 1]
        self.assertIn("subtitles", fc_value)
        # Intermediate label [vcat] feeds into subtitle filter → [vout]
        self.assertIn("[vcat]", fc_value)
        self.assertIn("[vout]", fc_value)

    def test_custom_profile(self):
        """Test FFmpegService with custom profile."""
        custom_profile = RenderProfile(
            name="custom",
            width=720,
            height=1280,
            fps=24,
            crf=20,
            preset="fast"
        )
        service = FFmpegService(custom_profile)
        
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=None,
            subtitle_path=None,
            scene_durations=[5.0]
        )
        output_path = Path("/tmp/output.mp4")
        
        cmd = service.build_command(input_data, output_path)
        
        # Verify custom settings are in command string
        cmd_str = " ".join(cmd)
        self.assertIn("720", cmd_str)
        self.assertIn("1280", cmd_str)
        self.assertIn("24", cmd_str)
        self.assertIn("20", cmd_str)
        self.assertIn("fast", cmd_str)

    @patch("creator_service.ffmpeg_service._run_ffmpeg")
    @patch.object(FFmpegService, "_render_segment")
    @patch("pathlib.Path.rename")
    def test_render_success(self, mock_rename, mock_render_segment, mock_run_ffmpeg):
        """render() concatenates rendered segments and returns the output path."""
        def encode(cmd, timeout):
            import subprocess
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                            "color=size=1080x1920:rate=30:duration=5", "-c:v", "libx264",
                            "-threads", "1", "-preset", "ultrafast", cmd[-1]],
                           check=True, capture_output=True, timeout=30)
            return MagicMock(returncode=0, stderr="")
        mock_run_ffmpeg.side_effect = encode

        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=None,
            subtitle_path=None,
            scene_durations=[5.0],
        )
        output_path = Path("/tmp/output.mp4")

        result = self.service.render(input_data, output_path)

        self.assertEqual(result, output_path)
        # One scene -> one segment render, then a single concat ffmpeg call.
        mock_render_segment.assert_called_once()
        mock_run_ffmpeg.assert_called_once()
        # Temp output is atomically renamed to the final path on success.
        mock_rename.assert_called_once_with(output_path)

    @patch("creator_service.ffmpeg_service._run_ffmpeg")
    @patch.object(FFmpegService, "_render_segment")
    def test_render_failure(self, mock_render_segment, mock_run_ffmpeg):
        """render() raises when ffmpeg fails and the output is not valid."""
        mock_run_ffmpeg.return_value = MagicMock(
            returncode=1,
            stderr="FFmpeg error message",
        )

        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=None,
            subtitle_path=None,
            scene_durations=[5.0],
        )
        output_path = Path("/tmp/output.mp4")

        with self.assertRaises(RuntimeError) as cm:
            self.service.render(input_data, output_path)

        self.assertIn("FFmpeg render failed", str(cm.exception))

    def test_build_command_without_subtitles_no_vcat(self):
        """Without subtitles, concat goes directly to [vout] (no [vcat])."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png"), Path("/tmp/img2.png")],
            audio_path=None,
            subtitle_path=None,
            scene_durations=[3.0, 4.0]
        )
        output_path = Path("/tmp/output.mp4")

        cmd = self.service.build_command(input_data, output_path)

        self.assertNotIn("-vf", cmd)
        fc_idx = cmd.index("-filter_complex")
        fc_value = cmd[fc_idx + 1]
        self.assertNotIn("[vcat]", fc_value)
        self.assertIn("[vout]", fc_value)
        self.assertNotIn("subtitles", fc_value)

    def test_build_command_subtitle_path_escaping(self):
        """Special characters in subtitle path are escaped for FFmpeg."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png")],
            audio_path=None,
            subtitle_path=Path("/data/project:1/subs,file[0].srt"),
            scene_durations=[5.0]
        )
        output_path = Path("/tmp/output.mp4")

        cmd = self.service.build_command(input_data, output_path)

        fc_idx = cmd.index("-filter_complex")
        fc_value = cmd[fc_idx + 1]
        # Colon must be escaped
        self.assertIn("\\:", fc_value)
        # Original un-escaped colon should not appear in the subtitle part
        self.assertNotIn("project:1", fc_value)
        # Comma must be escaped
        self.assertIn("\\,", fc_value)
        self.assertNotIn("subs,file", fc_value)
        # Square brackets must be escaped
        self.assertIn("\\[", fc_value)
        self.assertIn("\\]", fc_value)
        self.assertNotIn("[0]", fc_value)

    def test_build_command_with_audio_and_subtitles(self):
        """Audio + subtitles: single filter_complex, correct stream mapping."""
        input_data = RenderInput(
            image_paths=[Path("/tmp/img1.png"), Path("/tmp/img2.png")],
            audio_path=Path("/tmp/audio.wav"),
            subtitle_path=Path("/tmp/subs.srt"),
            scene_durations=[3.0, 4.0]
        )
        output_path = Path("/tmp/output.mp4")

        cmd = self.service.build_command(input_data, output_path)

        # No separate -vf
        self.assertNotIn("-vf", cmd)
        # filter_complex has both concat and subtitles
        fc_idx = cmd.index("-filter_complex")
        fc_value = cmd[fc_idx + 1]
        self.assertIn("subtitles", fc_value)
        self.assertIn("[vcat]", fc_value)
        self.assertIn("[vout]", fc_value)
        # Audio stream mapped
        self.assertIn("-c:a", cmd)


if __name__ == "__main__":
    unittest.main()
