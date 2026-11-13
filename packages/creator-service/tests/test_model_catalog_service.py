# pyright: reportMissingImports=false

from typing import cast
from unittest.mock import AsyncMock

import pytest
from creator_provider.registry import ProviderRegistry
from creator_service.model_catalog_service import ModelCatalogService
from creator_service.model_health_service import ModelHealthResult, ModelStatus


@pytest.fixture
def registry():
    return ProviderRegistry.create_default()


@pytest.fixture
def health_service() -> AsyncMock:
    service = AsyncMock()
    status_by_provider = {
        "ollama": ModelStatus.HEALTHY,
        "stable-diffusion": ModelStatus.UNHEALTHY,
        "tts-qwen3": ModelStatus.UNKNOWN,
        "stt-whisper": ModelStatus.HEALTHY,
        # External providers resolve to their hostnames
        "api.openai.com": ModelStatus.UNKNOWN,
        "api.anthropic.com": ModelStatus.UNKNOWN,
        "generativelanguage.googleapis.com": ModelStatus.UNKNOWN,
        "api.stability.ai": ModelStatus.UNKNOWN,
        "api.elevenlabs.io": ModelStatus.UNKNOWN,
    }

    async def check_model(name: str, *, endpoint: str | None = None) -> ModelHealthResult:
        status = status_by_provider.get(name, ModelStatus.UNKNOWN)
        return ModelHealthResult(
            model_name=name,
            endpoint=endpoint or f"http://{name}:1234",
            status=status,
        )

    service.check_model.side_effect = check_model
    return service


class TestModelCatalogService:
    @pytest.mark.asyncio
    async def test_list_models_returns_all_categories(self, registry, health_service: AsyncMock):
        service = ModelCatalogService(registry, health_service)

        result = await service.list_models()

        assert set(result.keys()) == {"script_models", "image_models", "tts_models", "stt_models"}
        assert len(result["script_models"]) == 6  # qwen3-4b, gpt-4o-mini, claude-sonnet, gemini-flash, llama-3.3-70b, llama-4-scout
        assert len(result["image_models"]) == 9  # sd15, dall-e-3, sd3-medium, imagen-3, pollinations, placeholder, hf-flux-schnell, groq-svg, codex-gpt-image
        assert len(result["tts_models"]) == 5  # qwen3-tts, elevenlabs, openai-tts, edge-tts, cosyvoice-0.5b
        assert len(result["stt_models"]) == 3

        # Verify local models are still present and correctly labeled
        script_keys = {m["key"] for m in result["script_models"]}
        assert "qwen3-4b" in script_keys
        assert "gpt-4o-mini" in script_keys

        local_script = next(m for m in result["script_models"] if m["key"] == "qwen3-4b")
        assert local_script["label"] == "Qwen3 4B (Local)"
        assert local_script["provider_type"] == "ollama"
        assert local_script["is_local"] is True
        assert local_script["requires_gpu"] is True
        assert local_script["status"] == "available"

        local_image = next(m for m in result["image_models"] if m["key"] == "sd15")
        assert local_image["status"] == "unavailable"
        assert local_image["label"] == "Stable Diffusion 1.5 (Local)"

        local_tts = next(m for m in result["tts_models"] if m["key"] == "qwen3-tts")
        assert local_tts["status"] == "unknown"
        assert local_tts["label"] == "Qwen3 TTS (Local)"

        stt_model = result["stt_models"][0]
        assert stt_model["label"] == "Whisper Small (Local)"

        # Verify remote models
        remote_image = next(m for m in result["image_models"] if m["key"] == "dall-e-3")
        assert remote_image["label"] == "DALL-E 3 (Remote)"
        assert remote_image["is_local"] is False

        remote_tts = next(m for m in result["tts_models"] if m["key"] == "elevenlabs-multilingual-v2")
        assert remote_tts["label"] == "ElevenLabs Multilingual v2 (Remote)"
        assert remote_tts["is_local"] is False

    @pytest.mark.asyncio
    async def test_list_models_filters_script_category(self, registry, health_service: AsyncMock):
        service = ModelCatalogService(registry, health_service)

        result = await service.list_models(category="script")

        assert len(result["script_models"]) == 6
        script_keys = {m["key"] for m in result["script_models"]}
        assert "qwen3-4b" in script_keys
        assert result["image_models"] == []
        assert result["tts_models"] == []
        assert result["stt_models"] == []

    @pytest.mark.asyncio
    async def test_list_models_filters_image_category(self, registry, health_service: AsyncMock):
        service = ModelCatalogService(registry, health_service)

        result = await service.list_models(category="image")

        assert len(result["image_models"]) == 9
        image_keys = {m["key"] for m in result["image_models"]}
        assert "sd15" in image_keys
        assert "dall-e-3" in image_keys
        assert result["script_models"] == []
        assert result["tts_models"] == []
        assert result["stt_models"] == []

    @pytest.mark.asyncio
    async def test_get_status_returns_expected_shape(self, registry, health_service: AsyncMock):
        service = ModelCatalogService(registry, health_service)

        result = await service.get_status()

        assert set(result.keys()) == {"providers", "gpu_lock"}
        assert isinstance(result["providers"], list)

        assert len(result["providers"]) == 17

        provider = result["providers"][0]
        assert set(provider.keys()) == {"name", "healthy", "status"}
        assert "endpoint" not in provider, "endpoint must not be exposed in status response"

        # Verify local providers are present
        provider_names = {p["name"] for p in result["providers"]}
        assert "ollama" in provider_names
        assert "stable-diffusion" in provider_names
        assert "tts-qwen3" in provider_names
        assert "stt-whisper" in provider_names

        assert result["gpu_lock"] == {"active": False}

    @pytest.mark.asyncio
    async def test_model_entries_have_required_fields(self, registry, health_service: AsyncMock):
        service = ModelCatalogService(registry, health_service)

        result = await service.list_models()

        required_fields = {
            "key",
            "label",
            "provider_type",
            "is_local",
            "requires_gpu",
            "status",
            "default_params",
        }
        all_entries = (
            result["script_models"]
            + result["image_models"]
            + result["tts_models"]
            + result["stt_models"]
        )

        for entry in all_entries:
            assert set(entry.keys()) == required_fields

    @pytest.mark.asyncio
    async def test_health_key_matches_health_service_keys(
        self, registry, health_service: AsyncMock
    ):
        service = ModelCatalogService(registry, health_service)

        await service.list_models()

        called_keys = {call.args[0] for call in health_service.check_model.call_args_list}
        # Local providers use Docker hostnames, remote providers use API hostnames
        expected_keys = {
            "ollama", "stable-diffusion", "tts-qwen3", "stt-whisper",
            "api.openai.com", "api.anthropic.com",
            "generativelanguage.googleapis.com",
            "api.stability.ai", "api.elevenlabs.io",
            "api.groq.com", "router.huggingface.co",
            "tts-cosyvoice",
        }
        assert called_keys == expected_keys

    @pytest.mark.asyncio
    async def test_configured_remote_providers_are_unverified(self, registry, health_service: AsyncMock):
        # Override health_service to return CONFIGURED for a remote provider
        original_side_effect = health_service.check_model.side_effect

        async def check_with_configured(name: str, *, endpoint: str | None = None) -> ModelHealthResult:
            if name == "api.openai.com":
                return ModelHealthResult(
                    model_name=name,
                    endpoint=f"http://{name}:1234",
                    status=ModelStatus.CONFIGURED,
                )
            return await original_side_effect(name, endpoint=endpoint)

        health_service.check_model.side_effect = check_with_configured
        service = ModelCatalogService(registry, health_service)

        result = await service.list_models()

        openai_models = [m for m in result["script_models"] if m["provider_type"] == "openai_llm"]
        assert len(openai_models) > 0
        assert openai_models[0]["status"] == "configured_unverified"

    @pytest.mark.asyncio
    async def test_get_status_configured_provider_is_unverified(self, registry, health_service: AsyncMock):
        original_side_effect = health_service.check_model.side_effect

        async def check_with_configured(name: str, *, endpoint: str | None = None) -> ModelHealthResult:
            if name == "api.openai.com":
                return ModelHealthResult(
                    model_name=name,
                    endpoint=f"http://{name}:1234",
                    status=ModelStatus.CONFIGURED,
                )
            return await original_side_effect(name, endpoint=endpoint)

        health_service.check_model.side_effect = check_with_configured
        service = ModelCatalogService(registry, health_service)

        result = await service.get_status()
        providers = cast(list[dict[str, object]], result["providers"])

        openai_provider = next(
            (p for p in providers if p["name"] == "api.openai.com"),
            None,
        )
        assert openai_provider is not None
        assert openai_provider["healthy"] is False
        assert openai_provider["status"] == "configured_unverified"
