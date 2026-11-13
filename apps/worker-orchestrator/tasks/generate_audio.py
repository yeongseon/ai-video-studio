# pyright: reportMissingImports=false
# ruff: noqa: E402

from __future__ import annotations

import logging
import os
import shutil
import wave
from decimal import Decimal

import re
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from celery_app import celery_app
from creator_domain.models.stage import RunStage
from creator_domain.sanitize import sanitize_path_component
from creator_provider.exceptions import ProviderError, ProviderTimeoutError, RateLimitError
from creator_provider.registry import get_default_registry
from creator_provider.word_timing_envelope import (
    WordTimingRow,
    envelope,
    read_envelope,
    write_atomic,
)
from creator_service.audio_service import audio_service as _audio_service
from creator_service.cost_config import COST_AUDIO_GENERATION
from creator_service.script_service import script_service as _script_service
from creator_service.telemetry import trace_task
from creator_service.usage_service import authorize_owned_provider_attempt, record_provider_call
from creator_service.spend_quote import MAX_AUDIO_DURATION_SECONDS, audio_reservation_quote
from tasks.task_runner import GpuLockContext, TaskContext, TaskResult, TaskRunnerConfig, run_task
from tasks.hard_timeout import production_task
from tasks.task_execution import TaskInputError

logger = logging.getLogger(__name__)
_ARTIFACT_ROOT = os.getenv("ARTIFACT_ROOT", "data/artifacts")
_TICKS_PER_SECOND = 10_000_000


def _strip_markdown_for_tts(text: str) -> str:
    """Remove markdown formatting markers that should not be read aloud by TTS.

    Strips:
    - Lines starting with ## (heading markers)
    - Lines starting with > (blockquote / display_text annotations)
    - Lines starting with ※ (note markers)
    - Emoji-only lines or emoji prefixes used as visual markers
    - [beat: ...] [emotion: ...] metadata annotations
    - Parenthetical stage directions like (마지막 5초 여운을 남기는 마무리)
    - Lines containing meta-instructions (어절, 나레이션, 이내여야)
    - Empty lines collapse to single newline
    """
    # First, remove inline [beat: X] [emotion: Y] markers
    text = re.sub(r"\[beat:\s*[^\]]*\]", "", text)
    text = re.sub(r"\[emotion:\s*[^\]]*\]", "", text)
    # Remove parenthetical stage directions (Korean or English)
    text = re.sub(r"\([^)]*(?:마지막|여운|마무리|beat|pause|silence|direction)[^)]*\)", "", text)
    # If LLM self-corrected (e.g. "수정된 버전"), only keep content BEFORE the revision marker
    revision_markers = ["수정된 버전", "다시 작성", "수정:", "revised version", "corrected version"]
    for marker in revision_markers:
        idx = text.lower().find(marker.lower())
        if idx > 50:  # Only cut if we have substantial content before the marker
            text = text[:idx]
            break
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Skip heading markers (## Hook, ## Body, ## Conclusion)
        if stripped.startswith("##"):
            continue
        # Skip blockquote display_text markers (> 🤯 text)
        if stripped.startswith(">"):
            continue
        # Skip note markers (※ instructions)
        if stripped.startswith("※"):
            continue
        # Skip lines that are only emojis/symbols
        if stripped and all(
            c in "🤯📝🙏❤️💔👆👉✨⭐🔥💡🎯" or ord(c) > 0x1F000 for c in stripped.replace(" ", "")
        ):
            continue
        # Skip meta-instruction lines the LLM may have regurgitated
        if any(
            kw in stripped
            for kw in (
                "어절",
                "나레이션 텍스트",
                "이내여야",
                "띄어쓰기 기준",
                "단어 수",
                "이내",
                "단어",
            )
        ):
            continue
        # Skip lines matching instruction patterns (N초 이내, N-N 단어, etc.)
        if re.search(r"\d+초\s*이내|\d+-\d+\s*단어|\d+~\d+어절", stripped):
            continue
        # Skip lines that are purely metadata after stripping
        if stripped and re.match(r"^[\s,]*$", stripped):
            continue
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def _get_audio_duration_seconds(path: str) -> float | None:
    """Get audio duration. Supports WAV natively; estimates MP3 from file size."""
    try:
        if path.endswith(".wav"):
            with wave.open(path, "rb") as wav_file:
                frame_rate = wav_file.getframerate()
                if frame_rate <= 0:
                    return None
                return wav_file.getnframes() / float(frame_rate)
        else:
            # Estimate MP3 duration from file size (~128kbps)
            file_size = os.path.getsize(path)
            return float(file_size) / (128 * 1000 / 8)
    except Exception:
        logger.warning("Could not determine audio duration for %s", path, exc_info=True)
        return None


def _merge_word_timing_sidecars(
    sections: list[tuple[str, str]], output_path: str, ffmpeg_service: object
) -> None:
    merged_path = Path(f"{output_path}.words.jsonl")
    merged_path.unlink(missing_ok=True)
    offset_ticks = 0
    merged_rows: list[WordTimingRow] = []
    spoken_sections: list[str] = []
    for audio_path, section_text in sections:
        sidecar = Path(f"{audio_path}.words.jsonl")
        if not sidecar.is_file():
            raise ProviderError(f"Missing Edge TTS word timing metadata: {sidecar}")
        document = read_envelope(sidecar, Path(audio_path), section_text)
        for row in document["words"]:
            merged_rows.append(
                {
                    **row,
                    "offset": row["offset"] + offset_ticks,
                }
            )
        spoken_sections.append(section_text)
        duration = ffmpeg_service.get_audio_duration(audio_path)
        offset_ticks += round(duration * _TICKS_PER_SECOND)
    write_atomic(
        merged_path,
        envelope(Path(output_path), "\n".join(spoken_sections), merged_rows),
    )


def _invalidate_word_timing_sidecar(audio_path: str) -> None:
    Path(f"{audio_path}.words.jsonl").unlink(missing_ok=True)


@celery_app.task(
    bind=True,
    base=production_task(
        frozenset({RunStage.VISUAL_ASSET_REVIEW.value, RunStage.AUDIO_GENERATING.value})
    ),
    autoretry_for=(ProviderTimeoutError, RateLimitError),
    retry_backoff=30,
    retry_jitter=True,
    max_retries=5,
    soft_time_limit=300,
    time_limit=360,
    name="generate_audio",
)
@trace_task("generate_audio")
def generate_audio(
    self,
    run_id: int,
    tts_model: str = "qwen3-tts",
    voice: str = "default",
) -> dict[str, object]:
    config = TaskRunnerConfig(
        task_name="generate_audio",
        allowed_stages=frozenset({RunStage.VISUAL_ASSET_REVIEW, RunStage.AUDIO_GENERATING}),
        safe_stages=frozenset(
            {RunStage.VISUAL_ASSET_REVIEW.value, RunStage.AUDIO_GENERATING.value}
        ),
        success_stage=RunStage.SUBTITLE_GENERATING.value,
        requires_free_space=True,
    )

    async def execute(ctx: TaskContext) -> TaskResult:
        draft = await _script_service.get_active_draft(run_id)
        if draft is None:
            raise TaskInputError(f"No script draft found for run {run_id}")

        script_text = (draft.markdown_content or "").strip()
        if not script_text and draft.structured_script:
            script_text = "\n".join(
                (section.display_text or section.text).strip()
                for section in draft.structured_script
                if (section.display_text or section.text).strip()
            )
        if not script_text:
            raise TaskInputError(f"Script draft for run {run_id} has no content")

        # Strip markdown formatting that should not be read aloud
        script_text = _strip_markdown_for_tts(script_text)
        if not script_text:
            raise TaskInputError(
                f"Script for run {run_id} has no speakable content after stripping markdown"
            )

        registry = get_default_registry()
        entry = registry.resolve(tts_model)
        provider = registry.get_provider(tts_model)
        delivery_cost = audio_reservation_quote(
            duration_seconds=MAX_AUDIO_DURATION_SECONDS,
            max_retries=0,
            cost_per_second=Decimal("0.0003"),
        )
        attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}:full"

        gpu_lock = GpuLockContext(ctx.task_id)
        if entry.requires_gpu:
            gpu_lock.acquire()

        # Use .mp3 for edge-tts, .wav for others
        ext = ".mp3" if entry.provider_type == "edge_tts" else ".wav"
        delivery_dir = (
            Path(_ARTIFACT_ROOT)
            / str(run_id)
            / "audio"
            / sanitize_path_component(
                ctx.task_id,
                label="task_id",
            )
        )
        audio_path = str(delivery_dir / f"audio{ext}")
        try:
            if delivery_dir.is_symlink():
                raise ValueError("Audio delivery directory is a symlink")
            os.makedirs(os.path.dirname(audio_path), exist_ok=True)

            # --- Per-section TTS with beat-aware rate variation ---
            per_section_generated = False
            if entry.provider_type == "edge_tts" and draft.structured_script:
                try:
                    from creator_service.recipe_profile import resolve_quality_profile

                    qp = resolve_quality_profile("shorts_default")
                    sections = draft.structured_script
                    section_audio_paths: list[str] = []
                    section_spoken_texts: list[str] = []
                    section_ids: list[str] = []

                    # Map section position to beat-appropriate rate
                    for idx, section in enumerate(sections):
                        section_text = (section.display_text or section.text or "").strip()
                        section_text = _strip_markdown_for_tts(section_text)
                        if not section_text:
                            continue

                        # Determine TTS rate based on section position/beat
                        if idx == 0:
                            rate = qp.tts_rate_hook
                        elif idx == len(sections) - 1:
                            rate = qp.tts_rate_conclusion
                        elif idx == len(sections) - 2 and len(sections) >= 4:
                            # Penultimate section = climax
                            rate = qp.tts_rate_climax
                        else:
                            rate = qp.tts_rate_body

                        section_path = str(delivery_dir / f"section_{idx}{ext}")
                        section_params = dict(entry.default_params or {})
                        section_params["output_path"] = section_path
                        section_params["rate"] = rate

                        attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}:section:{idx}"
                        if not await authorize_owned_provider_attempt(
                            ctx.reservation_owner_id,
                            attempt_id,
                            delivery_cost,
                        ):
                            raise ProviderError("Provider attempt exceeds approved spend")
                        ctx.mark_provider_started()
                        await provider.generate(section_text, voice=voice, params=section_params)
                        section_audio_paths.append(section_path)
                        section_spoken_texts.append(section_text)
                        section_ids.append(section.section_id or f"section_{idx}")
                        logger.debug("Section %d TTS: rate=%s len=%d", idx, rate, len(section_text))

                    if section_audio_paths:
                        # Store paragraph audio for render pipeline consumption
                        for sid, spath in zip(section_ids, section_audio_paths):
                            await _audio_service.create_paragraph_artifact(
                                run_id=run_id,
                                section_id=sid,
                                path=spath,
                                model_used=tts_model,
                                provider_type=entry.provider_type,
                                voice=voice,
                                idempotency_key=f"{ctx.task_id}_{sid}",
                            )
                        # Also create concatenated full audio as fallback
                        from creator_service.ffmpeg_service import FFmpegService

                        _ffmpeg = FFmpegService()
                        _ffmpeg.concatenate_audio(section_audio_paths, audio_path)
                        try:
                            _merge_word_timing_sidecars(
                                list(zip(section_audio_paths, section_spoken_texts)),
                                audio_path,
                                _ffmpeg,
                            )
                        except (ProviderError, OSError, RuntimeError, TypeError, ValueError):
                            _invalidate_word_timing_sidecar(audio_path)
                            logger.warning(
                                "Skipping unusable TTS timing metadata for run %d", run_id,
                                exc_info=True,
                            )
                        per_section_generated = True
                        logger.info(
                            "Per-section TTS generated for run %d (%d sections)",
                            run_id,
                            len(section_audio_paths),
                        )
                except SoftTimeLimitExceeded:
                    raise
                except ProviderError:
                    raise
                except Exception as sec_exc:
                    _invalidate_word_timing_sidecar(audio_path)
                    logger.warning(
                        "Per-section TTS failed for run %d, falling back to single-pass: %s",
                        run_id,
                        sec_exc,
                    )
                    per_section_generated = False

            # --- Fallback: single-pass TTS generation ---
            if not per_section_generated:
                _invalidate_word_timing_sidecar(audio_path)
                params = dict(entry.default_params or {})
                params["output_path"] = audio_path
                if entry.provider_type == "edge_tts":
                    try:
                        from creator_service.recipe_profile import resolve_quality_profile

                        qp = resolve_quality_profile("shorts_default")
                        params.update(qp.to_tts_params())
                        logger.info("Applied TTS rate=%s for run %d", params.get("rate"), run_id)
                    except Exception:
                        pass
                try:
                    attempt_id = f"{ctx.task_id}:{ctx.delivery_attempt}:full"
                    if not await authorize_owned_provider_attempt(
                        ctx.reservation_owner_id,
                        attempt_id,
                        delivery_cost,
                    ):
                        raise ProviderError("Provider attempt exceeds approved spend")
                    ctx.mark_provider_started()
                    await provider.generate(script_text, voice=voice, params=params)
                except ProviderError:
                    raise
                except (TimeoutError, ConnectionError) as exc:
                    raise ProviderTimeoutError(
                        f"Provider timed out during audio generation for run {run_id}"
                    ) from exc
                except SoftTimeLimitExceeded:
                    raise
                except Exception as exc:
                    message = str(exc).lower()
                    if (
                        "429" in message
                        or "rate limit" in message
                        or "too many requests" in message
                    ):
                        raise RateLimitError(
                            f"Provider rate limited audio generation for run {run_id}"
                        ) from exc
                    raise ProviderError(
                        f"Provider failed audio generation for run {run_id}"
                    ) from exc
        except BaseException:
            if delivery_dir.is_dir() and not delivery_dir.is_symlink():
                try:
                    shutil.rmtree(delivery_dir)
                except OSError:
                    logger.warning("Failed to remove delivery audio", exc_info=True)
            raise
        finally:
            if entry.requires_gpu:
                gpu_lock.release()

        audio_seconds = _get_audio_duration_seconds(audio_path)
        try:
            await record_provider_call(
                run_id,
                entry.provider_type,
                tts_model,
                "tts",
                audio_seconds=audio_seconds,
                cost_usd=(
                    Decimal(str(audio_seconds)) * Decimal("0.0003")
                    if audio_seconds is not None
                    else delivery_cost
                ),
                workspace_id=ctx.workspace_id,
                project_id=ctx.project_id,
                idempotency_key=ctx.task_id,
                reservation_owner_id=ctx.reservation_owner_id,
                reservation_attempt_id=attempt_id,
            )
            ctx.mark_provider_accounted()
        except Exception:
            if ctx.reservation_owner_id is not None:
                raise
            logger.warning("Failed to record provider usage", exc_info=True)

        from creator_service.artifact_storage_integration import store_artifact_file

        mime_type = "audio/mpeg" if audio_path.endswith(".mp3") else "audio/wav"
        uploaded = store_artifact_file(run_id, audio_path, mime_type)
        artifact = await _audio_service.create_artifact(
            run_id=run_id,
            path=audio_path,
            model_used=tts_model,
            provider_type=entry.provider_type,
            voice=voice,
            storage_provider=uploaded.storage_provider,
            storage_key=uploaded.key,
            idempotency_key=ctx.task_id,
        )

        return TaskResult(
            status="success",
            extra={
                "tts_model": tts_model,
                "voice": voice,
                "provider_type": entry.provider_type,
                "endpoint": entry.endpoint,
                "gpu_lock_acquired_at": gpu_lock.acquired_at,
                "gpu_lock_released_at": gpu_lock.released_at,
                "audio_artifact_id": artifact.id,
                "audio_path": artifact.path,
            },
        )

    return run_task(self, run_id, config, execute)
