from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from collections.abc import Sequence
from typing import Protocol

from creator_domain.exceptions import ChunkManifestValidationError

class _SourceRefContract(Protocol):
    @property
    def segment_index(self) -> int: ...

    @property
    def asset_id(self) -> int: ...

    @property
    def content_revision(self) -> int: ...

    @property
    def start_frame(self) -> int: ...

    @property
    def end_frame(self) -> int: ...


class _ChunkContract(Protocol):
    @property
    def index(self) -> int: ...

    @property
    def start_frame(self) -> int: ...

    @property
    def end_frame(self) -> int: ...

    @property
    def start_s(self) -> Fraction: ...

    @property
    def end_s(self) -> Fraction: ...

    @property
    def start_sample(self) -> int: ...

    @property
    def end_sample(self) -> int: ...

    @property
    def source_refs(self) -> Sequence[_SourceRefContract]: ...

    @property
    def frame_count(self) -> int: ...


class _OutputSpecContract(Protocol):
    @property
    def fps(self) -> int: ...


class _PolicyContract(Protocol):
    @property
    def max_chunk_frames(self) -> int: ...

    @property
    def max_chunk_count(self) -> int: ...

    @property
    def audio_sample_rate(self) -> int: ...


@dataclass(frozen=True, slots=True)
class ManifestValidationInput:
    total_frames: int
    fps: int
    output_spec: _OutputSpecContract
    policy: _PolicyContract
    chunks: Sequence[_ChunkContract]


def validate_manifest_contract(manifest: ManifestValidationInput) -> None:
    if manifest.output_spec.fps != manifest.fps:
        raise ChunkManifestValidationError("output specification fps must match manifest fps")
    if manifest.policy.audio_sample_rate < 1:
        raise ChunkManifestValidationError("policy audio sample rate must be positive")
    if manifest.total_frames < 1 or manifest.fps < 1 or not manifest.chunks:
        raise ChunkManifestValidationError(
            "manifest timeline must contain positive frames and chunks"
        )
    expected_start = 0
    for expected_index, chunk in enumerate(manifest.chunks):
        if chunk.index != expected_index:
            raise ChunkManifestValidationError(
                "chunk indices must be unique, ordered, and zero-based"
            )
        if chunk.start_frame < 0 or chunk.end_frame <= chunk.start_frame:
            raise ChunkManifestValidationError(
                "chunk frame ranges must be positive half-open intervals"
            )
        if chunk.start_frame != expected_start:
            raise ChunkManifestValidationError(
                "chunk coverage must be contiguous without gaps or overlaps"
            )
        if chunk.frame_count > manifest.policy.max_chunk_frames:
            raise ChunkManifestValidationError("chunk exceeds the configured frame bound")
        if chunk.start_s != Fraction(chunk.start_frame, manifest.fps) or chunk.end_s != Fraction(
            chunk.end_frame, manifest.fps
        ):
            raise ChunkManifestValidationError("chunk seconds must match exact frame boundaries")
        expected_start_sample = round(
            Fraction(chunk.start_frame * manifest.policy.audio_sample_rate, manifest.fps)
        )
        expected_end_sample = round(
            Fraction(chunk.end_frame * manifest.policy.audio_sample_rate, manifest.fps)
        )
        if (chunk.start_sample, chunk.end_sample) != (
            expected_start_sample,
            expected_end_sample,
        ):
            raise ChunkManifestValidationError(
                "audio samples must match exact frame boundaries"
            )
        if not chunk.source_refs:
            raise ChunkManifestValidationError(
                "each chunk must reference at least one source segment"
            )
        expected_source_start = chunk.start_frame
        for source_ref in chunk.source_refs:
            if source_ref.asset_id < 1 or source_ref.content_revision < 0:
                raise ChunkManifestValidationError(
                    "chunk source asset identity is missing or malformed"
                )
            if (
                source_ref.segment_index < 0
                or source_ref.start_frame < chunk.start_frame
                or source_ref.end_frame > chunk.end_frame
                or source_ref.end_frame <= source_ref.start_frame
            ):
                raise ChunkManifestValidationError(
                    "chunk source ranges must be positive and contained within the chunk"
                )
            if source_ref.start_frame != expected_source_start:
                raise ChunkManifestValidationError(
                    "chunk source coverage must be ordered without gaps or overlaps"
                )
            expected_source_start = source_ref.end_frame
        if expected_source_start != chunk.end_frame:
            raise ChunkManifestValidationError(
                "chunk source coverage must span the complete chunk"
            )
        expected_start = chunk.end_frame
    if expected_start != manifest.total_frames:
        raise ChunkManifestValidationError("chunks must cover the full planned frame count")
    if len(manifest.chunks) > manifest.policy.max_chunk_count:
        raise ChunkManifestValidationError("chunk count exceeds the configured policy")
