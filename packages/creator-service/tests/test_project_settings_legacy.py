from creator_domain.models.output_spec import EncodingProfile, OutputSpec
from creator_domain.models.project_settings import LEGACY_DEFAULT_ENCODING
from creator_service.render_profile import RenderProfile


def test_legacy_output_when_default_profile_is_resolved() -> None:
    # Given the existing render alias, when adapting its numeric settings.
    profile = RenderProfile.default()
    output = OutputSpec.from_render_profile(profile)
    encoding = EncodingProfile.from_render_profile(profile)
    # Then preserve the historical output, not the similarly named standard preset.
    assert (output.width, output.height, output.fps) == (1080, 1920, 30)
    assert (encoding.video_codec.value, encoding.audio_codec.value, encoding.crf, encoding.preset) == (
        "libx264", "aac", 18, "fast",
    )
    assert encoding.crf != EncodingProfile.standard().crf
    assert LEGACY_DEFAULT_ENCODING.model_dump(exclude={"name"}) == encoding.model_dump(exclude={"name"})
