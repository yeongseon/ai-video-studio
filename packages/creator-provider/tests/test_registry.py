import unittest
from unittest.mock import patch

from creator_provider.registry import ModelCatalogEntry, ProviderCategory, ProviderRegistry


class ProviderRegistryTests(unittest.TestCase):
    def test_register_model_and_resolve_for_all_categories(self) -> None:
        registry = ProviderRegistry()
        entries = [
            ModelCatalogEntry(
                model_key="test-llm",
                provider_type="ollama",
                endpoint="http://ollama:11434",
                category=ProviderCategory.LLM,
            ),
            ModelCatalogEntry(
                model_key="test-image",
                provider_type="sd_local",
                endpoint="http://stable-diffusion:7860",
                category=ProviderCategory.IMAGE,
            ),
            ModelCatalogEntry(
                model_key="test-tts",
                provider_type="qwen_tts",
                endpoint="http://tts-qwen3:8100",
                category=ProviderCategory.TTS,
            ),
            ModelCatalogEntry(
                model_key="test-stt",
                provider_type="whisper",
                endpoint="http://stt-whisper:8200",
                category=ProviderCategory.STT,
            ),
        ]

        for entry in entries:
            with self.subTest(model_key=entry.model_key):
                registry.register_model(entry)
                resolved = registry.resolve(entry.model_key)
                self.assertEqual(resolved, entry)

    def test_resolve_raises_key_error_for_unknown_model(self) -> None:
        registry = ProviderRegistry()

        with self.assertRaises(KeyError):
            registry.resolve("unknown-model")

    def test_list_models_with_category_filter(self) -> None:
        registry = ProviderRegistry()
        llm_entry = ModelCatalogEntry(
            model_key="test-llm",
            provider_type="ollama",
            endpoint="http://ollama:11434",
            category=ProviderCategory.LLM,
        )
        image_entry = ModelCatalogEntry(
            model_key="test-image",
            provider_type="sd_local",
            endpoint="http://stable-diffusion:7860",
            category=ProviderCategory.IMAGE,
        )

        registry.register_model(llm_entry)
        registry.register_model(image_entry)

        llm_models = registry.list_models(category=ProviderCategory.LLM)

        self.assertEqual(llm_models, [llm_entry])

    def test_create_default_returns_registry_with_default_entries(self) -> None:
        registry = ProviderRegistry.create_default()

        self.assertEqual(len(registry.list_models()), 23)
        self.assertEqual(registry.resolve("qwen3-4b").category, ProviderCategory.LLM)
        self.assertEqual(registry.resolve("sd15").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("qwen3-tts").category, ProviderCategory.TTS)
        self.assertEqual(registry.resolve("elevenlabs-multilingual-v2").category, ProviderCategory.TTS)
        self.assertEqual(registry.resolve("openai-tts-1").category, ProviderCategory.TTS)
        self.assertEqual(registry.resolve("whisper-small").category, ProviderCategory.STT)
        self.assertEqual(registry.resolve("script-timestamps").category, ProviderCategory.STT)
        self.assertEqual(registry.resolve("gpt-4o-mini").category, ProviderCategory.LLM)
        self.assertEqual(registry.resolve("claude-sonnet-4-20250514").category, ProviderCategory.LLM)
        self.assertEqual(registry.resolve("gemini-2.0-flash").category, ProviderCategory.LLM)
        self.assertEqual(registry.resolve("dall-e-3").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("sd3-medium").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("imagen-3").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("llama-3.3-70b-versatile").category, ProviderCategory.LLM)
        self.assertEqual(registry.resolve("groq-whisper-large-v3-turbo").category, ProviderCategory.STT)
        self.assertEqual(registry.resolve("pollinations").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("placeholder").category, ProviderCategory.IMAGE)
        self.assertEqual(registry.resolve("edge-tts").category, ProviderCategory.TTS)

    def test_get_provider_raises_key_error_for_unregistered_provider_type(self) -> None:
        registry = ProviderRegistry()
        registry.register_model(
            ModelCatalogEntry(
                model_key="test-llm",
                provider_type="unregistered",
                endpoint="http://localhost:9999",
                category=ProviderCategory.LLM,
            )
        )

        with self.assertRaises(KeyError):
            registry.get_provider("test-llm")

    def test_create_default_uses_default_local_endpoints_when_env_unset(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            registry = ProviderRegistry.create_default()

        self.assertEqual(registry.resolve("qwen3-4b").endpoint, "http://ollama:11434")
        self.assertEqual(registry.resolve("sd15").endpoint, "http://stable-diffusion:7860")
        self.assertEqual(registry.resolve("qwen3-tts").endpoint, "http://tts-qwen3:8100")
        self.assertEqual(registry.resolve("whisper-small").endpoint, "http://stt-whisper:8200")

    def test_create_default_uses_env_local_endpoints(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "OLLAMA_BASE_URL": "http://example-ollama:1234",
                "STABLE_DIFFUSION_BASE_URL": "http://example-sd:5678",
                "TTS_QWEN3_BASE_URL": "http://example-tts:9012",
                "STT_WHISPER_BASE_URL": "http://example-stt:3456",
            },
            clear=False,
        ):
            registry = ProviderRegistry.create_default()

        self.assertEqual(registry.resolve("qwen3-4b").endpoint, "http://example-ollama:1234")
        self.assertEqual(registry.resolve("sd15").endpoint, "http://example-sd:5678")
        self.assertEqual(registry.resolve("qwen3-tts").endpoint, "http://example-tts:9012")
        self.assertEqual(registry.resolve("whisper-small").endpoint, "http://example-stt:3456")


if __name__ == "__main__":
    unittest.main()
