"""BGM generation, SFX, and audio mixing service.

Generates ambient background music and sound effects using FFmpeg's built-in
audio generators and mixes them with narration audio for YouTube Shorts.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from .render_scratch import create_sfx_scratch

logger = logging.getLogger(__name__)

# FFmpeg lavfi SFX presets (short audio stingers for transitions)
_SFX_FILTERS: dict[str, str] = {
    "whoosh": "anoisesrc=d=0.5:c=white:r=44100,bandpass=f=2000:w=1000,afade=t=in:d=0.05,afade=t=out:st=0.25:d=0.25,volume=0.7",
    "sting": "sine=frequency=220:duration=0.4,afade=t=out:st=0.15:d=0.25,volume=1.5",
    "rise": "anoisesrc=d=1.0:c=pink:r=44100,lowpass=f=600,volume=0.6,afade=t=in:d=1.0",
    "drop": "sine=frequency=400:duration=0.6,asetrate=44100*0.5,afade=t=out:st=0.2:d=0.4,volume=1.0",
    "ping": "sine=frequency=1200:duration=0.3,afade=t=out:st=0.1:d=0.2,volume=0.5",
    "heartbeat": "sine=frequency=60:duration=0.8,asetpts=PTS-STARTPTS,volume=2.0,afade=t=out:st=0.4:d=0.4",
}
# FFmpeg lavfi filter presets for different moods
_MOOD_FILTERS: dict[str, str] = {
    "warm": "anoisesrc=d={duration}:c=pink:r=44100,lowpass=f=300,volume=0.4",
    "tense": "anoisesrc=d={duration}:c=brown:r=44100,lowpass=f=200,highpass=f=60,volume=0.5",
    "calm": "sine=frequency=174:duration={duration},lowpass=f=200,volume=0.3",
    "emotional": "anoisesrc=d={duration}:c=pink:r=44100,lowpass=f=250,highpass=f=80,volume=0.6",
    "dramatic": "anoisesrc=d={duration}:c=pink:r=44100,lowpass=f=400,highpass=f=100,volume=0.7,aecho=0.8:0.88:60:0.4",
    "suspense": "anoisesrc=d={duration}:c=brown:r=44100,lowpass=f=150,highpass=f=40,volume=0.5,aecho=0.8:0.9:1000:0.3",
}


class BgmService:
    """Service for generating and mixing background music."""

    def generate_ambient_bgm(
        self,
        duration_seconds: float,
        output_path: str,
        mood: str = "emotional",
    ) -> str:
        """Generate ambient BGM using FFmpeg lavfi filters.

        Args:
            duration_seconds: Length of BGM in seconds.
            output_path: Where to save the generated audio file.
            mood: One of 'warm', 'tense', 'calm', 'emotional'.

        Returns:
            The output path.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Validate duration to prevent resource exhaustion
        duration_seconds = max(1.0, min(float(duration_seconds), 600.0))

        filter_template = _MOOD_FILTERS.get(mood, _MOOD_FILTERS["emotional"])
        audio_filter = filter_template.format(duration=int(duration_seconds) + 1)

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            audio_filter,
            "-t",
            str(duration_seconds),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(out),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, start_new_session=True
        )
        if result.returncode != 0:
            logger.error("BGM generation failed: %s", result.stderr[-500:])
            raise RuntimeError(f"BGM generation failed: {result.stderr}")

        return str(out)

    def mix_audio_with_bgm(
        self,
        narration_path: str,
        bgm_path: str,
        output_path: str,
        bgm_volume: float = 0.25,
    ) -> str:
        """Mix narration audio with BGM using sidechain-style ducking.

        BGM volume ducks when narration is present (dynamic mixing).
        This creates a more professional sound where BGM fills pauses
        but stays quiet during speech.

        Args:
            narration_path: Path to narration audio file.
            bgm_path: Path to BGM audio file.
            output_path: Where to save mixed audio.
            bgm_volume: Base volume of BGM (0.0-1.0). BGM will duck below this during speech.

        Returns:
            The output path.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Validate bgm_volume to prevent filter injection
        bgm_volume = max(0.0, min(1.0, float(bgm_volume)))

        # Sidechain ducking: use sidechaincompress to duck BGM when narration is present
        # This gives a professional "radio" feel where BGM fills pauses naturally
        filter_complex = (
            f"[1:a]volume={bgm_volume}[bgm];"
            f"[bgm][0:a]sidechaincompress=threshold=0.02:ratio=6:attack=200:release=1000[ducked];"
            f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=2"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            narration_path,
            "-i",
            bgm_path,
            "-filter_complex",
            filter_complex,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(out),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, start_new_session=True
        )
        if result.returncode != 0:
            # Fallback: try simpler mix without sidechain (older FFmpeg may lack it)
            logger.warning("Sidechain ducking failed, falling back to simple mix: %s", result.stderr[-200:])
            filter_complex_simple = (
                f"[1:a]volume={bgm_volume}[bgm];"
                f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=2"
            )
            cmd_simple = [
                "ffmpeg",
                "-y",
                "-i",
                narration_path,
                "-i",
                bgm_path,
                "-filter_complex",
                filter_complex_simple,
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(out),
            ]
            result = subprocess.run(
                cmd_simple, capture_output=True, text=True, timeout=120, start_new_session=True
            )
            if result.returncode != 0:
                logger.error("Audio mixing failed: %s", result.stderr[-500:])
                raise RuntimeError(f"Audio mixing failed: {result.stderr}")

        return str(out)

    def generate_sfx(self, sfx_type: str, output_path: str) -> str:
        """Generate a short SFX audio clip using FFmpeg lavfi filters.

        Args:
            sfx_type: One of 'whoosh', 'sting', 'rise', 'drop', 'ping', 'heartbeat'.
            output_path: Where to save the generated SFX audio file.

        Returns:
            The output path.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        audio_filter = _SFX_FILTERS.get(sfx_type, _SFX_FILTERS["whoosh"])

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            audio_filter,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            str(out),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, start_new_session=True
        )
        if result.returncode != 0:
            logger.error("SFX generation failed (%s): %s", sfx_type, result.stderr[-300:])
            raise RuntimeError(f"SFX generation failed ({sfx_type}): {result.stderr}")

        return str(out)

    def mix_sfx_at_timestamps(
        self,
        base_audio_path: str,
        sfx_timestamps: list[tuple[float, str]],
        output_path: str,
        sfx_volume: float = 0.6,
    ) -> str:
        """Overlay SFX clips at specific timestamps on the base audio.

        Args:
            base_audio_path: Path to the main audio (narration+BGM mix).
            sfx_timestamps: List of (timestamp_seconds, sfx_type) pairs.
            output_path: Where to save the final mixed audio.
            sfx_volume: Volume of SFX overlay (0.0-1.0).

        Returns:
            The output path.
        """
        if not sfx_timestamps:
            return base_audio_path

        out = Path(output_path)
        sfx_volume = max(0.0, min(1.0, float(sfx_volume)))

        # Generate SFX clips to temp files
        tmp_dir = create_sfx_scratch(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        sfx_paths: list[tuple[float, str]] = []

        try:
            for i, (ts, sfx_type) in enumerate(sfx_timestamps):
                sfx_path = str(tmp_dir / f"sfx_{i}.mp3")
                try:
                    self.generate_sfx(sfx_type, sfx_path)
                    sfx_paths.append((ts, sfx_path))
                except RuntimeError:
                    logger.warning("Skipping SFX %s at %.1fs", sfx_type, ts)
                    continue

            if not sfx_paths:
                return base_audio_path

            # Build FFmpeg filter: overlay each SFX at its timestamp
            inputs = ["-i", base_audio_path]
            for _, sfx_path in sfx_paths:
                inputs.extend(["-i", sfx_path])

            # Filter: delay each SFX to its timestamp, mix all together
            filter_parts: list[str] = []
            mix_inputs: list[str] = ["[0:a]"]
            for i, (ts, _) in enumerate(sfx_paths):
                delay_ms = int(ts * 1000)
                filter_parts.append(
                    f"[{i+1}:a]volume={sfx_volume},adelay={delay_ms}|{delay_ms}[sfx{i}]"
                )
                mix_inputs.append(f"[sfx{i}]")

            filter_parts.append(
                f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=2"
            )

            cmd = [
                "ffmpeg",
                "-y",
                *inputs,
                "-filter_complex",
                ";".join(filter_parts),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "192k",
                str(out),
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, start_new_session=True
            )
            if result.returncode != 0:
                logger.warning("SFX mixing failed, returning original: %s", result.stderr[-300:])
                return base_audio_path

            return str(out)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def normalize_loudness(
        self,
        input_path: str,
        output_path: str,
        target_lufs: float = -14.0,
        true_peak: float = -1.0,
        lra: float = 11.0,
    ) -> str:
        """Apply EBU R128 loudness normalization using FFmpeg loudnorm filter.

        Ensures output audio is at a consistent loudness level suitable for
        YouTube Shorts (-14 to -16 LUFS). This should be the LAST audio
        processing step before muxing into video.

        Args:
            input_path: Path to input audio file.
            output_path: Where to save normalized audio.
            target_lufs: Target integrated loudness (default: -14 LUFS).
            true_peak: Maximum true peak level (default: -1 dBTP).
            lra: Target loudness range (default: 11 LU).

        Returns:
            The output path.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Validate parameters
        target_lufs = max(-70.0, min(-5.0, float(target_lufs)))
        true_peak = max(-9.0, min(0.0, float(true_peak)))
        lra = max(1.0, min(20.0, float(lra)))

        # Single-pass loudnorm (simpler, good enough for Shorts)
        audio_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-af",
            audio_filter,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(out),
        ]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120, start_new_session=True
        )
        if result.returncode != 0:
            logger.warning("Loudness normalization failed, returning original: %s", result.stderr[-300:])
            return input_path

        logger.info("Audio normalized to %.1f LUFS: %s", target_lufs, out)
        return str(out)

# Module-level singleton
bgm_service = BgmService()
