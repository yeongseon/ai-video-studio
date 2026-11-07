from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final

from pydantic import BaseModel, ValidationError as PydanticValidationError

from creator_domain.exceptions import ChunkManifestValidationError

from .output_spec import AudioCodec, EncodingProfile, OutputSpec, VideoCodec

DEFAULT_MAX_CHUNK_SECONDS: Final = 90
DEFAULT_MAX_CHUNK_COUNT: Final = 1_000
DEFAULT_AUDIO_SAMPLE_RATE: Final = 48_000


def validate_snapshot(factory: Callable[[], BaseModel]) -> None:
    try:
        _ = factory()
    except PydanticValidationError as error:
        raise ChunkManifestValidationError("invalid chunk manifest snapshot") from error


@dataclass(frozen=True, slots=True)
class ChunkPolicy:
    max_chunk_seconds: int | None = DEFAULT_MAX_CHUNK_SECONDS
    max_chunk_frames: int | None = None
    max_chunk_count: int = DEFAULT_MAX_CHUNK_COUNT
    audio_sample_rate: int = DEFAULT_AUDIO_SAMPLE_RATE

    def __post_init__(self) -> None:
        if self.max_chunk_seconds is not None and self.max_chunk_seconds < 1:
            raise ChunkManifestValidationError("max_chunk_seconds must be positive")
        if self.max_chunk_frames is not None and self.max_chunk_frames < 1:
            raise ChunkManifestValidationError("max_chunk_frames must be positive")
        if self.max_chunk_seconds is None and self.max_chunk_frames is None:
            raise ChunkManifestValidationError("a chunk duration or frame bound is required")
        if self.max_chunk_count < 1:
            raise ChunkManifestValidationError("max_chunk_count must be positive")
        if self.audio_sample_rate < 1:
            raise ChunkManifestValidationError("audio_sample_rate must be positive")

    def resolve(self, fps: int) -> ResolvedChunkPolicy:
        second_bound = None if self.max_chunk_seconds is None else self.max_chunk_seconds * fps
        bounds = [bound for bound in (second_bound, self.max_chunk_frames) if bound is not None]
        return ResolvedChunkPolicy(
            max_chunk_frames=min(bounds),
            max_chunk_count=self.max_chunk_count,
            audio_sample_rate=self.audio_sample_rate,
        )


@dataclass(frozen=True, slots=True)
class ResolvedChunkPolicy:
    max_chunk_frames: int
    max_chunk_count: int
    audio_sample_rate: int


@dataclass(frozen=True, slots=True)
class ChunkOutputSpec:
    width: int
    height: int
    fps: int

    def __post_init__(self) -> None:
        validate_snapshot(lambda: OutputSpec(width=self.width, height=self.height, fps=self.fps))

    @classmethod
    def from_output_spec(cls, value: OutputSpec) -> ChunkOutputSpec:
        return cls(width=value.width, height=value.height, fps=value.fps)


@dataclass(frozen=True, slots=True)
class ChunkEncodingProfile:
    name: str
    video_codec: VideoCodec
    audio_codec: AudioCodec
    crf: int
    preset: str

    def __post_init__(self) -> None:
        validate_snapshot(
            lambda: EncodingProfile(
                name=self.name,
                video_codec=self.video_codec,
                audio_codec=self.audio_codec,
                crf=self.crf,
                preset=self.preset,
            )
        )

    @classmethod
    def from_encoding_profile(cls, value: EncodingProfile) -> ChunkEncodingProfile:
        return cls(
            name=value.name,
            video_codec=value.video_codec,
            audio_codec=value.audio_codec,
            crf=value.crf,
            preset=value.preset,
        )
