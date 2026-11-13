import logging
import math
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .render_profile import RenderProfile
from .media_integrity import MediaExpectation, validate_media
from .render_frame_budget import allocate_frames
from .render_scratch import audio_concat_list, create_render_scratch

logger = logging.getLogger(__name__)


@dataclass
class RenderInput:
    image_paths: list[Path]  # ordered scene images
    audio_path: Path | None  # narration audio
    subtitle_path: Path | None  # SRT subtitle file
    scene_durations: list[float]  # duration per scene in seconds
    scene_transitions: list[str] | None = None  # optional per-scene transition style override


def _escape_subtitle_path(path: Path) -> str:
    """Escape special characters in subtitle path for FFmpeg filter syntax.

    FFmpeg filter strings treat ``\\``, ``:``, ``'``, ``;``, ``,``, ``[``,
    and ``]`` as syntax characters.  They must be escaped with a backslash so
    the path is interpreted literally.
    """
    s = str(path)
    # Order matters: escape backslashes first, then the rest.
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    return s




def _validate_durations(durations: list[float]) -> None:
    """Reject non-positive, NaN, or infinite durations."""
    for i, d in enumerate(durations):
        if not isinstance(d, (int, float)):
            raise ValueError(f"scene_durations[{i}] must be numeric, got {type(d).__name__}")
        if math.isnan(d) or math.isinf(d):
            raise ValueError(f"scene_durations[{i}] must be finite, got {d}")
        if d <= 0:
            raise ValueError(f"scene_durations[{i}] must be positive, got {d}")


def _run_ffmpeg(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg with proper process-group isolation and timeout cleanup.

    Uses start_new_session=True to isolate from Celery's signal handling,
    and os.killpg() on timeout to ensure all child processes are cleaned up.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Kill the entire process group to clean up any children
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.kill()  # fallback
        proc.wait()
        raise
    return subprocess.CompletedProcess(
        args=cmd, returncode=proc.returncode, stdout=stdout, stderr=stderr
    )


class FFmpegService:
    def __init__(self, profile: RenderProfile | None = None):
        self.profile = profile or RenderProfile.default()

    def build_command(self, input_data: RenderInput, output_path: Path) -> list[str]:
        """Build FFmpeg command for rendering video from scenes.

        NOTE: This builds the legacy single-pass filter_complex command.
        The actual render() method now uses a two-pass concat demuxer approach
        for stability in containerized environments (FFmpeg 7.x).
        This method is retained for backward compatibility and testing.
        """
        cmd = ["ffmpeg", "-y", "-nostdin"]

        if len(input_data.image_paths) != len(input_data.scene_durations):
            raise ValueError(
                f"image_paths ({len(input_data.image_paths)}) and "
                f"scene_durations ({len(input_data.scene_durations)}) must have equal length"
            )
        if not input_data.image_paths:
            raise ValueError("image_paths must not be empty")

        # --- inputs --------------------------------------------------------
        filter_parts: list[str] = []
        for i, (img, dur) in enumerate(
            zip(input_data.image_paths, input_data.scene_durations, strict=False),
        ):
            cmd.extend(["-loop", "1", "-t", str(dur), "-i", str(img)])
            filter_parts.append(
                f"[{i}:v]format=yuv420p,"
                f"scale={self.profile.width}:{self.profile.height}"
                f":force_original_aspect_ratio=decrease,"
                f"pad={self.profile.width}:{self.profile.height}"
                f":(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1[v{i}]"
            )

        # --- concat --------------------------------------------------------
        concat_inputs = "".join(f"[v{i}]" for i in range(len(input_data.image_paths)))

        need_subs = self.profile.burn_subtitles and input_data.subtitle_path is not None

        if need_subs:
            filter_parts.append(
                f"{concat_inputs}concat=n={len(input_data.image_paths)}:v=1:a=0[vcat]"
            )
            assert input_data.subtitle_path is not None
            escaped = _escape_subtitle_path(input_data.subtitle_path)
            if str(input_data.subtitle_path).endswith(".ass"):
                filter_parts.append(f"[vcat]ass={escaped}[vout]")
            else:
                filter_parts.append(
                    f"[vcat]subtitles={escaped}"
                    f":force_style='FontSize={self.profile.subtitle_font_size}'[vout]"
                )
        else:
            filter_parts.append(
                f"{concat_inputs}concat=n={len(input_data.image_paths)}:v=1:a=0[vout]"
            )

        cmd.extend(["-filter_complex", ";".join(filter_parts)])

        # --- stream mapping ------------------------------------------------
        if input_data.audio_path:
            cmd.extend(["-i", str(input_data.audio_path)])
            cmd.extend(["-map", "[vout]", "-map", f"{len(input_data.image_paths)}:a"])
        else:
            cmd.extend(["-map", "[vout]"])

        # --- codec settings ------------------------------------------------
        cmd.extend(
            [
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                self.profile.video_codec.value,
                "-crf",
                str(self.profile.crf),
                "-preset",
                self.profile.preset,
                "-threads",
                "2",
                "-r",
                str(self.profile.fps),
            ]
        )

        if input_data.audio_path:
            cmd.extend(["-c:a", self.profile.audio_codec.value, "-shortest"])

        cmd.append(str(output_path))
        return cmd

    def _render_segment(self, image_path: Path, duration: float, output_path: Path, transition_override: str | None = None) -> None:
        """Render a single scene image into a video segment (.ts).

        Each segment is a self-contained H.264 MPEG-TS clip with the correct
        resolution, pixel format, and frame rate. These segments can be
        concatenated losslessly via the concat demuxer.

        When transition_style is KEN_BURNS, applies a subtle zoom-pan effect
        with fade in/out for professional-looking scene transitions.

        Args:
            transition_override: If provided, use this style instead of profile default.
        """
        # Build video filter based on transition style
        effective_style = transition_override or self.profile.transition_style.value
        if effective_style == "ken_burns":
            # Full Ken Burns: zoom from 1.0 to 1.10 with subtle pan + fade in/out
            # Use 2x target resolution as input for zoom headroom (not 8000px — too slow)
            zoom_input_w = self.profile.width * 2
            total_frames = max(1, int(duration * self.profile.fps))
            fade_d = 0.3
            fade_out_start = max(0, duration - fade_d)
            vf = (
                f"scale={zoom_input_w}:-1,"
                f"zoompan=z='1+0.10*on/{total_frames}':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={self.profile.width}x{self.profile.height}:fps={self.profile.fps},"
                f"fade=t=in:st=0:d={fade_d},"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_d},"
                f"format=yuv420p,setsar=1"
            )
        elif effective_style == "ken_burns_lite":
            # Lightweight Ken Burns: native resolution, subtle 5% zoom, no upscale
            # Much faster than full ken_burns — safe for containerized workers
            total_frames = max(1, int(duration * self.profile.fps))
            fade_d = 0.25
            fade_out_start = max(0, duration - fade_d)
            vf = (
                f"scale={self.profile.width}:{self.profile.height}"
                f":force_original_aspect_ratio=decrease,"
                f"pad={self.profile.width}:{self.profile.height}"
                f":(ow-iw)/2:(oh-ih)/2,"
                f"zoompan=z='1+0.05*on/{total_frames}':"
                f"x='iw/2-(iw/zoom/2)':"
                f"y='ih/2-(ih/zoom/2)':"
                f"d={total_frames}:s={self.profile.width}x{self.profile.height}:fps={self.profile.fps},"
                f"fade=t=in:st=0:d={fade_d},"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_d},"
                f"format=yuv420p,setsar=1"
            )
        elif effective_style == "fade":
            # Fade only: static image with fade in/out
            fade_d = 0.3
            fade_out_start = max(0, duration - fade_d)
            vf = (
                f"format=yuv420p,"
                f"scale={self.profile.width}:{self.profile.height}"
                f":force_original_aspect_ratio=decrease,"
                f"pad={self.profile.width}:{self.profile.height}"
                f":(ow-iw)/2:(oh-ih)/2,"
                f"fade=t=in:st=0:d={fade_d},"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_d},"
                f"setsar=1"
            )
        else:
            # Cut: no motion, no fade — just scale and pad
            vf = (
                f"format=yuv420p,"
                f"scale={self.profile.width}:{self.profile.height}"
                f":force_original_aspect_ratio=decrease,"
                f"pad={self.profile.width}:{self.profile.height}"
                f":(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1"
            )

        cmd = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-loop",
            "1",
            "-t",
            str(duration),
            "-i",
            str(image_path),
            "-vf",
            f"{vf},fps={self.profile.fps},trim=duration={duration},setpts=PTS-STARTPTS",
            "-c:v",
            self.profile.video_codec.value,
            "-crf",
            str(self.profile.crf),
            "-preset",
            self.profile.preset,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(self.profile.fps),
            # Force keyframe at start for clean concat
            "-force_key_frames",
            "expr:eq(n,0)",
            "-frames:v", str(round(duration * self.profile.fps)),
            "-f", "mp4", str(output_path),
        ]
        result = _run_ffmpeg(cmd, timeout=180)  # zoompan needs more time
        if result.returncode != 0:
            logger.error("Segment render failed for %s: %s", image_path, result.stderr[-500:])
            raise RuntimeError(
                f"Segment render failed for {image_path.name} "
                f"(rc={result.returncode}): {result.stderr[-200:]}"
            )

    def render(self, input_data: RenderInput, output_path: Path) -> Path:
        """Execute FFmpeg render using two-pass concat demuxer approach.

        Pass 1: Render each scene image into an individual .ts video segment.
        Pass 2: Concatenate segments via concat demuxer, add audio + subtitles.

        This avoids the multi-input filter_complex graph that causes
        'Error reinitializing filters!' in FFmpeg 7.x under Celery workers.

        Renders to a unique temporary file, then atomically renames on success.
        Every accepted output passes integrity validation; nonzero exits fail.
        """
        import gc
        import shutil

        if len(input_data.image_paths) != len(input_data.scene_durations):
            raise ValueError(
                f"image_paths ({len(input_data.image_paths)}) and "
                f"scene_durations ({len(input_data.scene_durations)}) must have equal length"
            )
        if not input_data.image_paths:
            raise ValueError("image_paths must not be empty")
        _validate_durations(input_data.scene_durations)

        # Create temp directory for intermediate segments
        tmp_dir = create_render_scratch(output_path, ".render_tmp_")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_output: Path | None = None

        try:
            # --- Pass 1: Render each scene as individual .ts segment ---
            segment_paths: list[Path] = []
            gc.collect()

            frame_budgets = allocate_frames(input_data.scene_durations, self.profile.fps)
            for i, (img, dur) in enumerate(
                zip(input_data.image_paths, input_data.scene_durations, strict=False),
            ):
                seg_path = tmp_dir / f"seg_{i:04d}.ts"
                # Use per-scene transition override if provided
                trans_override = None
                if input_data.scene_transitions and i < len(input_data.scene_transitions):
                    trans_override = input_data.scene_transitions[i]
                self._render_segment(img, frame_budgets[i] / self.profile.fps, seg_path, transition_override=trans_override)
                segment_paths.append(seg_path)
            # --- Pass 2: Concatenate segments + audio + subtitles ---
            concat_list_path = tmp_dir / "concat.txt"
            with open(concat_list_path, "w") as f:
                for seg, budget in zip(segment_paths, frame_budgets, strict=True):
                    f.write(f"file '{seg.name}'\nduration {budget / self.profile.fps:.12f}\n")

            unique_suffix = f".tmp.{os.getpid()}.{os.urandom(4).hex()}.mp4"
            tmp_output = output_path.with_suffix(unique_suffix)

            need_subs = self.profile.burn_subtitles and input_data.subtitle_path is not None

            cmd: list[str] = [
                "ffmpeg",
                "-y",
                "-nostdin",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
            ]

            if input_data.audio_path:
                cmd.extend(["-i", str(input_data.audio_path)])

            if need_subs:
                assert input_data.subtitle_path is not None
                escaped = _escape_subtitle_path(input_data.subtitle_path)
                if str(input_data.subtitle_path).endswith(".ass"):
                    cmd.extend(["-vf", f"ass={escaped}"])
                else:
                    cmd.extend(
                        [
                            "-vf",
                            f"subtitles={escaped}"
                            f":force_style='FontSize={self.profile.subtitle_font_size}'",
                        ]
                    )
                # Must re-encode video when burning subtitles
                cmd.extend(
                    [
                        "-c:v",
                        self.profile.video_codec.value,
                        "-crf",
                        str(self.profile.crf),
                        "-preset",
                        self.profile.preset,
                        "-pix_fmt",
                        "yuv420p",
                        "-r",
                        str(self.profile.fps),
                    ]
                )
            else:
                # No subtitles — copy video stream (lossless concat)
                cmd.extend(["-c:v", "copy"])

            if input_data.audio_path:
                cmd.extend(
                    [
                        "-c:a",
                        self.profile.audio_codec.value,
                        "-map",
                        "0:v",
                        "-map",
                        "1:a",
                        "-shortest",
                    ]
                )
            else:
                cmd.extend(["-map", "0:v"])

            cmd.extend(["-t", str(sum(input_data.scene_durations)), str(tmp_output)])
            logger.debug("FFmpeg concat command: %s", " ".join(str(c) for c in cmd))

            gc.collect()
            try:
                result = _run_ffmpeg(cmd, timeout=300)
            except subprocess.TimeoutExpired:
                if tmp_output.exists():
                    tmp_output.unlink()
                raise

            if result.returncode != 0:
                # Truly failed — clean up temp file
                if tmp_output.exists():
                    tmp_output.unlink()
                logger.error(
                    "FFmpeg concat stderr: %s",
                    result.stderr[-2000:] if result.stderr else "empty",
                )
                raise RuntimeError(
                    f"FFmpeg render failed (rc={result.returncode}): {result.stderr}"
                )

            validate_media(tmp_output, MediaExpectation(
                self.profile, sum(input_data.scene_durations), input_data.audio_path is not None,
                len(input_data.scene_durations),
            ))
            # Success — atomic rename from temp to final
            tmp_output.rename(output_path)
            return output_path

        finally:
            if tmp_output is not None:
                tmp_output.unlink(missing_ok=True)
            # Clean up intermediate segments
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # -- Per-paragraph helper methods -----------------------------------------

    def get_audio_duration(self, audio_path: str) -> float:
        """Probe audio file duration in seconds using ffprobe."""
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed for {audio_path}: {result.stderr}")
        return float(result.stdout.strip())

    def concatenate_audio(self, audio_paths: list[str], output_path: str) -> Path:
        """Concatenate multiple audio files using FFmpeg concat demuxer.

        Creates a temporary concat list file, runs FFmpeg, and returns the
        output path.  The caller is responsible for ensuring input files exist.
        """
        out = Path(output_path)
        with audio_concat_list(out) as concat_list:
            out.parent.mkdir(parents=True, exist_ok=True)
            with concat_list.open("w") as f:
                for p in audio_paths:
                    abs_p = str(Path(p).resolve())
                    escaped = abs_p.replace("'", "'\\''")
                    f.write(f"file '{escaped}'\n")
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c",
                "copy",
                str(out),
            ]
            result = _run_ffmpeg(cmd, timeout=120)
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg concat failed: {result.stderr}")
        return out

    def merge_subtitles(
        self,
        subtitle_paths: list[str],
        durations: list[float],
        output_path: str,
    ) -> Path:
        """Merge multiple SRT files into one, adjusting timestamps.

        Each subtitle file's timestamps are offset by the cumulative duration
        of all preceding paragraphs.  This produces a single SRT file suitable
        for rendering onto the concatenated audio track.
        """
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        merged_entries: list[str] = []
        entry_index = 1
        cumulative_offset = 0.0

        for srt_path, dur in zip(subtitle_paths, durations, strict=False):
            path = Path(srt_path)
            if not path.exists():
                cumulative_offset += dur
                continue

            content = path.read_text(encoding="utf-8")
            blocks = content.strip().split("\n\n")

            for block in blocks:
                lines = block.strip().split("\n")
                if len(lines) < 3:
                    continue  # skip malformed blocks

                # Parse timestamp line: "00:00:01,000 --> 00:00:03,500"
                ts_line = lines[1]
                parts = ts_line.split(" --> ")
                if len(parts) != 2:
                    continue

                start = _parse_srt_ts(parts[0].strip()) + cumulative_offset
                end = _parse_srt_ts(parts[1].strip()) + cumulative_offset
                text = "\n".join(lines[2:])

                merged_entries.append(
                    f"{entry_index}\n{_format_srt_ts(start)} --> {_format_srt_ts(end)}\n{text}"
                )
                entry_index += 1

            cumulative_offset += dur

        out.write_text("\n\n".join(merged_entries) + "\n", encoding="utf-8")
        return out

    def convert_srt_to_ass(
        self,
        srt_path: str | Path,
        ass_path: str | Path,
        width: int | None = None,
        height: int | None = None,
        font_size: int | None = None,
        emphasis_words: list[str] | None = None,
        emphasis_color: str = "&H0000FFFF",
    ) -> Path:
        """Convert SRT subtitle to ASS format with styled rendering.

        Produces bottom-center subtitles with:
        - White bold text
        - Black outline (2px)
        - Semi-transparent black background box (BorderStyle=3)
        - Large font for mobile readability
        - Optional keyword emphasis (yellow highlighted words)
        """
        import re

        w = width or self.profile.width
        h = height or self.profile.height
        fs = font_size or self.profile.subtitle_font_size
        out = Path(ass_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        # Parse SRT
        srt_content = Path(srt_path).read_text(encoding="utf-8")
        blocks = srt_content.strip().split("\n\n")

        # Emphasis font size (30% larger)
        efs = int(fs * 1.3)

        # ASS header with Default + Emphasis styles
        ass_lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {w}",
            f"PlayResY: {h}",
            "WrapStyle: 0",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            f"Style: Default,NanumGothic,{fs},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,3,2,0,2,10,10,30,1",
            f"Style: Emphasis,NanumGothic,{efs},{emphasis_color},&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,3,2,0,2,10,10,30,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        for block in blocks:
            lines = block.strip().split("\n")
            if len(lines) < 3:
                continue
            ts_line = lines[1]
            parts = ts_line.split(" --> ")
            if len(parts) != 2:
                continue
            start = _srt_ts_to_ass(parts[0].strip())
            end = _srt_ts_to_ass(parts[1].strip())
            text = "\\N".join(lines[2:])  # ASS newline

            # Escape braces FIRST to prevent ASS override injection from subtitle text.
            # Must happen before emphasis insertion so the override tags we add aren't escaped.
            text = text.replace("{", "\uff5b").replace("}", "\uff5d")

            # Apply keyword emphasis AFTER escaping (our tags use real braces).
            if emphasis_words:
                for word in emphasis_words:
                    pattern = re.escape(word)
                    text = re.sub(
                        pattern,
                        lambda m: f"{{\\c{emphasis_color}\\fs{efs}\\b1}}{m.group(0)}{{\\r}}",
                        text,
                        flags=re.IGNORECASE,
                    )

            ass_lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

        out.write_text("\n".join(ass_lines) + "\n", encoding="utf-8")
        return out


def _parse_srt_ts(ts: str) -> float:
    """Parse SRT timestamp 'HH:MM:SS,mmm' to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])


def _format_srt_ts(seconds: float) -> str:
    """Format seconds as SRT timestamp 'HH:MM:SS,mmm'."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    ms = int((s - int(s)) * 1000)
    return f"{h:02d}:{m:02d}:{int(s):02d},{ms:03d}"


def _srt_ts_to_ass(ts: str) -> str:
    """Convert SRT timestamp 'HH:MM:SS,mmm' to ASS format 'H:MM:SS.cc'."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h = int(parts[0])
    m = int(parts[1])
    s_parts = parts[2].split(".")
    s = int(s_parts[0])
    # ASS uses centiseconds (2 digits)
    ms = int(s_parts[1]) if len(s_parts) > 1 else 0
    cs = ms // 10
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
