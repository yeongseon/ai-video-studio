from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final, TypedDict

from pydantic import TypeAdapter, ValidationError as PydanticValidationError

from creator_domain.exceptions import ChunkManifestValidationError

from .chunk_identity import ApprovalIdentity
from .chunk_policy import (
    ChunkEncodingProfile,
    ChunkOutputSpec,
    ResolvedChunkPolicy,
)
from .output_spec import AudioCodec, VideoCodec

SCHEMA_VERSION: Final = 1


class _FractionJson(TypedDict):
    numerator: int
    denominator: int


class _SourceRefJson(TypedDict):
    segment_index: int
    source: str
    asset_id: int
    content_revision: int
    start_frame: int
    end_frame: int


class _ChunkJson(TypedDict):
    index: int
    start_frame: int
    end_frame: int
    start_s: _FractionJson
    end_s: _FractionJson
    start_sample: int
    end_sample: int
    source_refs: list[_SourceRefJson]


class _OutputSpecJson(TypedDict):
    width: int
    height: int
    fps: int


class _EncodingProfileJson(TypedDict):
    name: str
    video_codec: str
    audio_codec: str
    crf: int
    preset: str


class _PolicyJson(TypedDict):
    max_chunk_frames: int
    max_chunk_count: int
    audio_sample_rate: int


class _ApprovalJson(TypedDict):
    timeline_id: str
    revision: int


class _ManifestJson(TypedDict):
    schema_version: int
    approval: _ApprovalJson
    total_frames: int
    fps: int
    output_spec: _OutputSpecJson
    encoding_profile: _EncodingProfileJson
    policy: _PolicyJson
    chunks: list[_ChunkJson]
    manifest_id: str


_MANIFEST_ADAPTER: Final = TypeAdapter(_ManifestJson)


@dataclass(frozen=True, slots=True)
class ChunkSourceRef:
    segment_index: int
    source: str
    asset_id: int
    content_revision: int
    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class RenderChunk:
    index: int
    start_frame: int
    end_frame: int
    start_s: Fraction
    end_s: Fraction
    start_sample: int
    end_sample: int
    source_refs: tuple[ChunkSourceRef, ...]

    @property
    def frame_count(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    schema_version: int
    approval: ApprovalIdentity
    total_frames: int
    fps: int
    output_spec: ChunkOutputSpec
    encoding_profile: ChunkEncodingProfile
    policy: ResolvedChunkPolicy
    chunks: tuple[RenderChunk, ...]
    manifest_id: str = field(init=False)

    def __post_init__(self) -> None:
        self._validate()
        object.__setattr__(self, "manifest_id", hashlib.sha256(self._canonical_bytes()).hexdigest())

    def _validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ChunkManifestValidationError(f"unsupported schema_version: {self.schema_version}")
        self.approval.validate()
        if self.total_frames < 1 or self.fps < 1 or not self.chunks:
            raise ChunkManifestValidationError(
                "manifest timeline must contain positive frames and chunks"
            )
        expected_start = 0
        for expected_index, chunk in enumerate(self.chunks):
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
            if chunk.frame_count > self.policy.max_chunk_frames:
                raise ChunkManifestValidationError("chunk exceeds the configured frame bound")
            if chunk.start_s != Fraction(chunk.start_frame, self.fps) or chunk.end_s != Fraction(
                chunk.end_frame, self.fps
            ):
                raise ChunkManifestValidationError(
                    "chunk seconds must match exact frame boundaries"
                )
            expected_start_sample = round(
                Fraction(chunk.start_frame * self.policy.audio_sample_rate, self.fps)
            )
            expected_end_sample = round(
                Fraction(chunk.end_frame * self.policy.audio_sample_rate, self.fps)
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
            for source_ref in chunk.source_refs:
                if source_ref.asset_id < 1 or source_ref.content_revision < 0:
                    raise ChunkManifestValidationError(
                        "chunk source asset identity is missing or malformed"
                    )
            expected_start = chunk.end_frame
        if expected_start != self.total_frames:
            raise ChunkManifestValidationError("chunks must cover the full planned frame count")
        if len(self.chunks) > self.policy.max_chunk_count:
            raise ChunkManifestValidationError("chunk count exceeds the configured policy")

    def _content(self) -> dict[str, int | dict[str, int | str] | list[_ChunkJson]]:
        return {
            "schema_version": self.schema_version,
            "approval": {
                "timeline_id": self.approval.timeline_id,
                "revision": self.approval.revision,
            },
            "total_frames": self.total_frames,
            "fps": self.fps,
            "output_spec": {
                "width": self.output_spec.width,
                "height": self.output_spec.height,
                "fps": self.output_spec.fps,
            },
            "encoding_profile": {
                "name": self.encoding_profile.name,
                "video_codec": self.encoding_profile.video_codec.value,
                "audio_codec": self.encoding_profile.audio_codec.value,
                "crf": self.encoding_profile.crf,
                "preset": self.encoding_profile.preset,
            },
            "policy": {
                "max_chunk_frames": self.policy.max_chunk_frames,
                "max_chunk_count": self.policy.max_chunk_count,
                "audio_sample_rate": self.policy.audio_sample_rate,
            },
            "chunks": [_chunk_json(chunk) for chunk in self.chunks],
        }

    def _canonical_bytes(self) -> bytes:
        return json.dumps(self._content(), sort_keys=True, separators=(",", ":")).encode()

    def to_json(self) -> str:
        return json.dumps({**self._content(), "manifest_id": self.manifest_id}, sort_keys=True)

    @classmethod
    def from_json(cls, value: str) -> ChunkManifest:
        try:
            data = _MANIFEST_ADAPTER.validate_json(value)
            manifest = cls(
                schema_version=data["schema_version"],
                approval=ApprovalIdentity(**data["approval"]),
                total_frames=data["total_frames"],
                fps=data["fps"],
                output_spec=ChunkOutputSpec(**data["output_spec"]),
                encoding_profile=ChunkEncodingProfile(
                    name=data["encoding_profile"]["name"],
                    video_codec=VideoCodec(data["encoding_profile"]["video_codec"]),
                    audio_codec=AudioCodec(data["encoding_profile"]["audio_codec"]),
                    crf=data["encoding_profile"]["crf"],
                    preset=data["encoding_profile"]["preset"],
                ),
                policy=ResolvedChunkPolicy(**data["policy"]),
                chunks=tuple(_chunk_from_json(chunk) for chunk in data["chunks"]),
            )
        except (PydanticValidationError, TypeError, ValueError) as error:
            raise ChunkManifestValidationError("invalid serialized chunk manifest") from error
        if data["manifest_id"] != manifest.manifest_id:
            raise ChunkManifestValidationError("manifest_id does not match canonical content")
        return manifest


def _fraction_json(value: Fraction) -> _FractionJson:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _chunk_json(chunk: RenderChunk) -> _ChunkJson:
    return {
        "index": chunk.index,
        "start_frame": chunk.start_frame,
        "end_frame": chunk.end_frame,
        "start_s": _fraction_json(chunk.start_s),
        "end_s": _fraction_json(chunk.end_s),
        "start_sample": chunk.start_sample,
        "end_sample": chunk.end_sample,
        "source_refs": [
            {
                "segment_index": ref.segment_index,
                "source": ref.source,
                "asset_id": ref.asset_id,
                "content_revision": ref.content_revision,
                "start_frame": ref.start_frame,
                "end_frame": ref.end_frame,
            }
            for ref in chunk.source_refs
        ],
    }


def _chunk_from_json(data: _ChunkJson) -> RenderChunk:
    return RenderChunk(
        index=data["index"],
        start_frame=data["start_frame"],
        end_frame=data["end_frame"],
        start_s=Fraction(**data["start_s"]),
        end_s=Fraction(**data["end_s"]),
        start_sample=data["start_sample"],
        end_sample=data["end_sample"],
        source_refs=tuple(ChunkSourceRef(**ref) for ref in data["source_refs"]),
    )
