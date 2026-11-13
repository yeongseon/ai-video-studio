import os
from typing import Any

from creator_domain.provider_catalog import (
    ModelCatalogEntry as ModelCatalogEntry,
    ProviderCategory as ProviderCategory,
)


class ProviderRegistry:
    def __init__(self):
        self._catalog: dict[str, ModelCatalogEntry] = {}
        self._providers: dict[str, Any] = {}

    def register_model(self, entry: ModelCatalogEntry) -> None:
        self._catalog[entry.model_key] = entry

    def register_provider(self, provider_type: str, provider_class: type) -> None:
        self._providers[provider_type] = provider_class

    def resolve(self, model_key: str) -> ModelCatalogEntry:
        if model_key not in self._catalog:
            raise KeyError(f"Model '{model_key}' not found in catalog")
        return self._catalog[model_key]

    def get_provider(self, model_key: str) -> Any:
        entry = self.resolve(model_key)
        provider_class = self._providers.get(entry.provider_type)
        if provider_class is None:
            raise KeyError(f"Provider type '{entry.provider_type}' not registered")
        return provider_class(endpoint=entry.endpoint, model_key=model_key)

    def list_models(self, category: ProviderCategory | None = None) -> list[ModelCatalogEntry]:
        entries = list(self._catalog.values())
        if category:
            entries = [entry for entry in entries if entry.category == category]
        return entries

    @classmethod
    def create_default(cls) -> "ProviderRegistry":
        from creator_provider.image.dalle_provider import DalleProvider
        from creator_provider.image.imagen_provider import ImagenProvider
        from creator_provider.image.sd_local_provider import SDLocalProvider
        from creator_provider.image.stability_provider import StabilityProvider
        from creator_provider.llm.anthropic_provider import AnthropicProvider
        from creator_provider.llm.gemini_provider import GeminiProvider
        from creator_provider.llm.ollama_provider import OllamaProvider
        from creator_provider.llm.openai_provider import OpenAIProvider
        from creator_provider.stt.whisper_provider import WhisperSTTProvider
        from creator_provider.stt.script_timestamp_aligner import ScriptTimestampAligner
        from creator_provider.tts.elevenlabs_provider import ElevenLabsProvider
        from creator_provider.tts.openai_tts_provider import OpenAITTSProvider
        from creator_provider.tts.qwen_tts_provider import QwenTTSProvider
        from creator_provider.llm.groq_provider import GroqLLMProvider
        from creator_provider.stt.groq_stt_provider import GroqSTTProvider
        from creator_provider.image.pollinations_provider import PollinationsProvider
        from creator_provider.tts.edge_tts_provider import EdgeTTSProvider
        from creator_provider.image.placeholder_provider import PlaceholderImageProvider
        from creator_provider.image.huggingface_provider import HuggingFaceImageProvider
        from creator_provider.image.groq_svg_provider import GroqSvgImageProvider
        from creator_provider.image.codex_provider import CodexProvider
        from creator_provider.tts.cosyvoice_provider import CosyVoiceProvider

        registry = cls()
        registry.register_provider("ollama", OllamaProvider)
        registry.register_provider("sd_local", SDLocalProvider)
        registry.register_provider("qwen_tts", QwenTTSProvider)
        registry.register_provider("elevenlabs_tts", ElevenLabsProvider)
        registry.register_provider("openai_tts", OpenAITTSProvider)
        registry.register_provider("whisper", WhisperSTTProvider)
        registry.register_provider("script_timestamp_aligner", ScriptTimestampAligner)
        registry.register_provider("openai_llm", OpenAIProvider)
        registry.register_provider("anthropic_llm", AnthropicProvider)
        registry.register_provider("gemini_llm", GeminiProvider)
        registry.register_provider("openai_image", DalleProvider)
        registry.register_provider("stability_image", StabilityProvider)
        registry.register_provider("google_image", ImagenProvider)
        registry.register_provider("groq_llm", GroqLLMProvider)
        registry.register_provider("groq_stt", GroqSTTProvider)
        registry.register_provider("pollinations_image", PollinationsProvider)
        registry.register_provider("edge_tts", EdgeTTSProvider)
        registry.register_provider("cosyvoice_tts", CosyVoiceProvider)
        registry.register_provider("placeholder_image", PlaceholderImageProvider)
        registry.register_provider("huggingface_image", HuggingFaceImageProvider)
        registry.register_provider("groq_svg_image", GroqSvgImageProvider)
        registry.register_provider("codex_image", CodexProvider)
        registry.register_model(
            ModelCatalogEntry(
                model_key="qwen3-4b",
                provider_type="ollama",
                endpoint=os.getenv("OLLAMA_BASE_URL", "http://ollama:11434"),
                category=ProviderCategory.LLM,
                requires_gpu=True,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="sd15",
                provider_type="sd_local",
                endpoint=os.getenv("STABLE_DIFFUSION_BASE_URL", "http://stable-diffusion:7860"),
                category=ProviderCategory.IMAGE,
                requires_gpu=True,
                is_local=True,
                default_params={
                    "steps": 25,
                    "cfg_scale": 7,
                    "sampler_name": "DPM++ 2M Karras",
                    "negative_prompt": (
                        "low quality, worst quality, blurry, out of focus, "
                        "ugly, deformed, disfigured, watermark, text, "
                        "signature, poorly drawn, bad anatomy, extra limbs"
                    ),
                },
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="qwen3-tts",
                provider_type="qwen_tts",
                endpoint=os.getenv("TTS_QWEN3_BASE_URL", "http://tts-qwen3:8100"),
                category=ProviderCategory.TTS,
                requires_gpu=True,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="elevenlabs-multilingual-v2",
                provider_type="elevenlabs_tts",
                endpoint="https://api.elevenlabs.io",
                category=ProviderCategory.TTS,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="openai-tts-1",
                provider_type="openai_tts",
                endpoint="https://api.openai.com",
                category=ProviderCategory.TTS,
                requires_gpu=False,
                is_local=False,
                default_params={"voice": "alloy"},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="whisper-small",
                provider_type="whisper",
                endpoint=os.getenv("STT_WHISPER_BASE_URL", "http://stt-whisper:8200"),
                category=ProviderCategory.STT,
                requires_gpu=True,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="script-timestamps",
                provider_type="script_timestamp_aligner",
                endpoint="",
                category=ProviderCategory.STT,
                requires_gpu=False,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="gpt-4o-mini",
                provider_type="openai_llm",
                endpoint="https://api.openai.com",
                category=ProviderCategory.LLM,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="claude-sonnet-4-20250514",
                provider_type="anthropic_llm",
                endpoint="https://api.anthropic.com",
                category=ProviderCategory.LLM,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="gemini-2.0-flash",
                provider_type="gemini_llm",
                endpoint="https://generativelanguage.googleapis.com",
                category=ProviderCategory.LLM,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="dall-e-3",
                provider_type="openai_image",
                endpoint="https://api.openai.com",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"width": 1024, "height": 1792},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="sd3-medium",
                provider_type="stability_image",
                endpoint="https://api.stability.ai",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"aspect_ratio": "9:16"},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="imagen-3",
                provider_type="google_image",
                endpoint="https://generativelanguage.googleapis.com",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"width": 1024, "height": 1792},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="llama-3.3-70b-versatile",
                provider_type="groq_llm",
                endpoint="https://api.groq.com/openai/v1",
                category=ProviderCategory.LLM,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="meta-llama/llama-4-scout-17b-16e-instruct",
                provider_type="groq_llm",
                endpoint="https://api.groq.com/openai/v1",
                category=ProviderCategory.LLM,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="groq-whisper-large-v3-turbo",
                provider_type="groq_stt",
                endpoint="https://api.groq.com/openai/v1",
                category=ProviderCategory.STT,
                requires_gpu=False,
                is_local=False,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="pollinations",
                provider_type="pollinations_image",
                endpoint="https://image.pollinations.ai",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"width": 1024, "height": 1792},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="placeholder",
                provider_type="placeholder_image",
                endpoint="local",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=True,
                default_params={"width": 1024, "height": 1792},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="edge-tts",
                provider_type="edge_tts",
                endpoint="local",
                category=ProviderCategory.TTS,
                requires_gpu=False,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="cosyvoice-0.5b",
                provider_type="cosyvoice_tts",
                endpoint=os.getenv("TTS_COSYVOICE_BASE_URL", "http://tts-cosyvoice:50000"),
                category=ProviderCategory.TTS,
                requires_gpu=True,
                is_local=True,
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="hf-flux-schnell",
                provider_type="huggingface_image",
                endpoint="https://router.huggingface.co",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"width": 768, "height": 1344},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="groq-svg",
                provider_type="groq_svg_image",
                endpoint="https://api.groq.com",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
                default_params={"width": 1080, "height": 1920},
            )
        )
        registry.register_model(
            ModelCatalogEntry(
                model_key="codex-gpt-image",
                provider_type="codex_image",
                endpoint="https://chatgpt.com/backend-api/codex",
                category=ProviderCategory.IMAGE,
                requires_gpu=False,
                is_local=False,
            )
        )
        return registry


_default_registry: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    """Return a cached default ProviderRegistry singleton.

    Thread-safety note: In an async single-threaded event loop (the normal
    FastAPI/uvicorn deployment), this is safe. If called from multiple OS threads,
    the worst case is duplicate initialization (benign — both instances are identical
    and one will be garbage-collected). No lock is added to avoid unnecessary overhead.
    """
    global _default_registry
    if _default_registry is None:
        _default_registry = ProviderRegistry.create_default()
    return _default_registry
