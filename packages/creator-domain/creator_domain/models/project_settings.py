"""Versioned project choices; legacy encoding is a read-only compatibility value."""

from typing import ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from creator_domain.models.output_spec import EncodingProfile, OutputSpec, StrEnum


class ContentFormat(StrEnum):
    SHORT = "short"
    LONG = "long"


SUPPORTED_CONTENT_FORMATS: Final = frozenset({ContentFormat.SHORT})


class ProjectSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    content_format: ContentFormat = ContentFormat.SHORT
    output_preset: str = Field(default="short_vertical", max_length=50)
    encoding_profile: str = Field(default="standard", max_length=50)
    target_duration_seconds: float | None = Field(
        default=None, strict=True, gt=0, allow_inf_nan=False,
    )

    @field_validator("content_format")
    @classmethod
    def supported_format(cls, value: ContentFormat) -> ContentFormat:
        if value not in SUPPORTED_CONTENT_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_CONTENT_FORMATS))
            message = f"Content format {value.value!r} is not available yet; supported: {supported}"
            raise ValueError(message)
        return value

    @field_validator("output_preset")
    @classmethod
    def supported_output(cls, value: str) -> str:
        try:
            OutputSpec.preset(value)
        except ValueError as error:
            message = f"{error}; supported: short_vertical, short_square, short_landscape"
            raise ValueError(message) from error
        return value

    @field_validator("encoding_profile")
    @classmethod
    def supported_encoding(cls, value: str) -> str:
        try:
            EncodingProfile.by_name(value)
        except ValueError as error:
            message = f"{error}; supported: preview, standard, high_quality"
            raise ValueError(message) from error
        return value


class LegacyProjectSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    content_format: Literal[ContentFormat.SHORT] = ContentFormat.SHORT
    output_preset: Literal["short_vertical"] = "short_vertical"
    encoding_profile: Literal["legacy_default"] = "legacy_default"
    target_duration_seconds: None = None


LEGACY_DEFAULT_ENCODING: Final = EncodingProfile(name="legacy_default", crf=18, preset="fast")


class EffectiveSettings(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    settings: ProjectSettings | LegacyProjectSettings
    source: Literal["explicit", "legacy_default"]


def resolve_effective_settings(stored: ProjectSettings | None) -> EffectiveSettings:
    if stored is None:
        return EffectiveSettings(settings=LegacyProjectSettings(), source="legacy_default")
    return EffectiveSettings(settings=stored, source="explicit")
