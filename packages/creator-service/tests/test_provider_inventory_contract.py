from unittest.mock import patch

import httpx
import pytest
from creator_provider.registry import ModelCatalogEntry, ProviderCategory, ProviderRegistry
from creator_service.model_catalog_service import ModelCatalogService
from creator_service.model_health_service import ModelHealthResult, ModelHealthService, ModelStatus
from creator_service.provider_config_view import resolve_provider_config_view
from creator_service.provider_facts import resolve_provider_facts
from creator_service.provider_metadata import PROVIDER_ALIASES
from creator_service.provider_readiness import resolve_setup_provider_facts
from creator_service.setup_wizard import ModelCategory


def remote_inventory() -> ProviderRegistry:
    registry = ProviderRegistry()
    for entry in ProviderRegistry.create_default().list_models():
        if not entry.is_local:
            registry.register_model(entry)
    return registry


@pytest.mark.asyncio
async def test_every_registered_remote_appears_in_provider_config() -> None:
    # Given the actual shipped remote inventory.
    registry = remote_inventory()
    # When configuration is read without generating anything.
    with patch("httpx.AsyncClient", side_effect=AssertionError("unexpected provider traffic")):
        view = await resolve_provider_config_view(
            registry=registry, health_service=ModelHealthService(), configured_providers=(),
        )
    # Then metadata omissions cannot silently hide registered providers.
    assert {s.provider for s in view.providers} == {
        PROVIDER_ALIASES.get(e.provider_type, e.provider_type) for e in registry.list_models()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("token_env", ["HF_TOKEN", "HUGGINGFACE_TOKEN", None])
async def test_huggingface_optional_token_is_reported_without_validation(
    monkeypatch: pytest.MonkeyPatch, token_env: str | None,
) -> None:
    # Given HF's actual optional credential names, absent from the generic API-key list.
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    if token_env is not None:
        monkeypatch.setenv(token_env, "test-hf-token")
    registry = ProviderRegistry()
    registry.register_model(ProviderRegistry.create_default().resolve("hf-flux-schnell"))
    # When all readiness consumers resolve the same registry.
    with patch("httpx.AsyncClient", side_effect=AssertionError("unexpected provider traffic")):
        catalog = await ModelCatalogService(registry, ModelHealthService()).list_models()
        view = await resolve_provider_config_view(
            registry=registry, health_service=ModelHealthService(), configured_providers=(),
        )
        setup = await resolve_setup_provider_facts(
            registry=registry, health_service=ModelHealthService(), configured_remote_providers=(),
        )
    # Then token presence permits an unverified attempt, never a healthy claim.
    expected = "unknown" if token_env is None else "configured_unverified"
    assert catalog["image_models"][0]["status"] == expected
    assert len(view.providers) == 1
    state = view.providers[0]
    assert (state.status.value, state.is_local, state.env_var) == (expected, False, "HF_TOKEN")
    assert state.configured is True
    assert state.unavailable_categories == ()
    assert setup.category_status.get(ModelCategory.IMAGE, ()) == (
        () if token_env is None else ("huggingface_image",)
    )


@pytest.mark.asyncio
async def test_codex_private_auth_stays_explicitly_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a private file-auth provider, unrelated to OPENAI_API_KEY.
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    registry = ProviderRegistry()
    registry.register_model(ProviderRegistry.create_default().resolve("codex-gpt-image"))
    # When configuration and setup are inspected without reading private auth or calling gti.
    with patch("httpx.AsyncClient", side_effect=AssertionError("unexpected provider traffic")):
        view = await resolve_provider_config_view(
            registry=registry, health_service=ModelHealthService(), configured_providers={"openai"},
        )
        setup = await resolve_setup_provider_facts(
            registry=registry, health_service=ModelHealthService(), configured_remote_providers={"openai"},
        )
    # Then the inventory discloses the unsupported provider without claiming credentials or readiness.
    assert len(view.providers) == 1
    state = view.providers[0]
    assert (state.provider, state.status.value, state.configured, state.env_var) == (
        "codex_image", "unknown", False, None,
    )
    assert setup.unknown_providers == ("codex_image",)
    assert not setup.category_status.get(ModelCategory.IMAGE)
    assert "codex_image" not in setup.unhealthy_providers


@pytest.mark.asyncio
async def test_registry_default_endpoint_wins_over_health_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an explicit registry endpoint and a conflicting health-service default.
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://other-service:9911/stale")
    entry = ModelCatalogEntry("script", "ollama", "http://ollama:11434/", ProviderCategory.LLM)
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    client_class = httpx.AsyncClient
    # When the actual health reader handles the registry entry.
    with patch("creator_service.model_health_service.httpx.AsyncClient",
               side_effect=lambda **kwargs: client_class(transport=httpx.MockTransport(respond))):
        await resolve_provider_facts([entry], ModelHealthService())
    # Then the registry endpoint is authoritative, even when it equals the shipped default.
    assert requests == ["http://ollama:11434/api/tags"]


@pytest.mark.asyncio
async def test_registered_provider_facts_use_canonical_capability_identity() -> None:
    # Given the real registry, including its legacy local flag for Edge TTS.
    registry = ProviderRegistry.create_default()

    class ProtocolHealth:
        async def check_model(
            self, model_name: str, *, endpoint: str | None = None,
        ) -> ModelHealthResult:
            return ModelHealthResult(model_name, endpoint or model_name, ModelStatus.UNKNOWN)

    # When facts normalize all shipped provider identities.
    facts = await resolve_provider_facts(registry.list_models(), ProtocolHealth())
    # Then model aliases share provider identity and keyless/local semantics stay truthful.
    by_provider = {fact.provider: fact for fact in facts}
    assert {fact.provider for fact in facts if fact.entry.provider_type.startswith("openai_")} == {"openai"}
    assert {fact.provider for fact in facts if fact.keyless} == {
        "placeholder_image", "edge_tts", "pollinations_image", "huggingface_image",
    }
    assert {fact.provider for fact in facts if fact.is_local} == {
        "ollama", "sd_local", "qwen_tts", "cosyvoice_tts", "whisper",
        "script_timestamp_aligner", "placeholder_image",
    }
    assert by_provider["codex_image"].status is ModelStatus.UNKNOWN
