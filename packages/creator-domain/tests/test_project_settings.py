import pytest
from pydantic import ValidationError

from creator_domain.models.output_spec import OutputSpec
from creator_domain.models.project_settings import ProjectSettings, resolve_effective_settings


@pytest.mark.parametrize("duration", [None, 0.5, 240, 3600])
@pytest.mark.parametrize("preset", ["short_vertical", "short_square", "short_landscape"])
@pytest.mark.parametrize("encoding", ["preview", "standard", "high_quality"])
def test_settings_roundtrip_when_supported(duration: float | None, preset: str, encoding: str) -> None:
    # Given supported independent product/output choices.
    settings = ProjectSettings(output_preset=preset, encoding_profile=encoding,
                               target_duration_seconds=duration)
    # When persisted as JSON and reloaded.
    restored = ProjectSettings.model_validate_json(settings.model_dump_json())
    # Then every choice survives without conflating geometry and format.
    assert restored == settings
    assert restored.content_format == "short"


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True, "x", "240"])
def test_duration_rejected_when_not_positive_finite_number(duration: float | str) -> None:
    # Given untrusted duration input, when parsing settings.
    with pytest.raises(ValidationError) as error:
        ProjectSettings.model_validate({"target_duration_seconds": duration})
    # Then validation identifies the duration field.
    assert error.value.errors()[0]["loc"] == ("target_duration_seconds",)


@pytest.mark.parametrize("field,value,message", [
    ("content_format", "long", "not available yet"),
    ("content_format", "other", "short"),
    ("output_preset", "unknown", "short_square"),
    ("encoding_profile", "unknown", "high_quality"),
    ("encoding_profile", "legacy_default", "standard"),
    ("schema_version", 2, "1"),
    ("workspace_id", 2, "Extra inputs"),
])
def test_settings_rejected_when_unsupported(field: str, value: str | int, message: str) -> None:
    # Given an unsupported field/value, when crossing the domain boundary.
    with pytest.raises(ValidationError, match=message):
        ProjectSettings.model_validate({field: value})


def test_settings_frozen_when_mutation_attempted() -> None:
    # Given a validated value object, when a caller attempts mutation.
    settings = ProjectSettings()
    with pytest.raises(ValidationError, match="frozen"):
        settings.output_preset = "short_square"


def test_effective_settings_when_project_is_legacy() -> None:
    # Given no stored settings, when resolving defaults.
    effective = resolve_effective_settings(None)
    output = OutputSpec.preset(effective.settings.output_preset)
    # Then preserve geometry and identify the non-standard legacy encoding.
    assert effective.source == "legacy_default"
    assert (output.width, output.height, output.fps) == (1080, 1920, 30)
    assert effective.settings.encoding_profile == "legacy_default"
    assert effective.settings.target_duration_seconds is None


def test_effective_settings_when_project_is_explicit() -> None:
    # Given settings unlike the default, when resolving effective settings.
    settings = ProjectSettings(output_preset="short_square", encoding_profile="high_quality",
                               target_duration_seconds=240)
    effective = resolve_effective_settings(settings)
    # Then retain the selected values and their provenance.
    assert effective.source == "explicit"
    assert effective.settings == settings
