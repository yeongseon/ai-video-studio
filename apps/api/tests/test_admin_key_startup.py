"""Tests for ADMIN_API_KEY startup validation (fail-fast in production)."""

import asyncio

import pytest
from creator_service.production_checks import ProductionConfigError
from shorts_api.lifecycle import validate_admin_api_key


def test_validate_raises_in_production_when_missing():
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(environment="production", admin_key="", _is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_raises_in_production_when_too_short():
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(environment="production", admin_key="short", _is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_raises_in_production_when_15_chars():
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(environment="production", admin_key="a" * 15, _is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_passes_in_production_with_16_char_key():
    validate_admin_api_key(environment="production", admin_key="a" * 16, _is_test_runtime=False)


def test_validate_passes_in_production_with_64_char_key():
    validate_admin_api_key(environment="production", admin_key="a" * 64, _is_test_runtime=False)


def test_validate_passes_in_development_when_missing():
    validate_admin_api_key(environment="development", admin_key="", _is_test_runtime=False)


def test_validate_passes_in_development_when_short():
    validate_admin_api_key(environment="development", admin_key="short", _is_test_runtime=False)


@pytest.mark.parametrize("environment", ["development", "production", "staging", "custom"])
@pytest.mark.parametrize("key", ["local-admin-key-change-me", "<a-strong-random-string>"])
def test_validate_rejects_public_example_in_every_environment(environment: str, key: str) -> None:
    # Given a publicly shipped value, even long enough for the production minimum.
    # When startup validates it, then the operator receives actionable configuration feedback.
    with pytest.raises(ProductionConfigError) as error:
        validate_admin_api_key(environment=environment, admin_key=key)
    message = str(error.value)
    for fragment in ("ADMIN_API_KEY", "publicly known example", "unset", "disabled", "openssl rand -hex 32"):
        assert fragment in message


def test_validate_rejects_public_example_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the normal environment-driven startup path.
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("ADMIN_API_KEY", "local-admin-key-change-me")
    # When / Then
    with pytest.raises(ProductionConfigError, match="ADMIN_API_KEY"):
        validate_admin_api_key()


def test_validate_skipped_when_test_runtime_true():
    """Explicit _is_test_runtime=True skips validation."""
    validate_admin_api_key(environment="production", admin_key="", _is_test_runtime=True)


def test_validate_normalizes_environment_case():
    """'Production' (mixed case) should still trigger validation."""
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(environment="Production", admin_key="", _is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_normalizes_environment_whitespace():
    """' production ' (whitespace) should still trigger validation."""
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(environment=" production ", admin_key="", _is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_env_driven_path(monkeypatch):
    """When no explicit args, reads from ENVIRONMENT and ADMIN_API_KEY env vars."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "tooshort")
    with pytest.raises(SystemExit) as exc_info:
        validate_admin_api_key(_is_test_runtime=False)
    assert exc_info.value.code == 1


def test_validate_env_driven_path_valid(monkeypatch):
    """Valid env vars should not raise."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("ADMIN_API_KEY", "a" * 32)
    validate_admin_api_key(_is_test_runtime=False)


def test_validate_no_pytest_env_bypass(monkeypatch):
    """PYTEST_CURRENT_TEST env var should NOT bypass validation in production."""
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "yes")
    with pytest.raises(SystemExit):
        validate_admin_api_key(environment="production", admin_key="", _is_test_runtime=False)


def test_lifespan_calls_validate(monkeypatch):
    """Lifespan should call validate_admin_api_key at startup."""
    from shorts_api.lifecycle import lifespan
    from unittest.mock import MagicMock

    called = []
    original_validate = validate_admin_api_key

    def _tracking_validate(**kwargs):
        called.append(kwargs)

    monkeypatch.setattr("shorts_api.lifecycle.validate_admin_api_key", _tracking_validate)

    app = MagicMock()

    async def _run():
        async with lifespan(app):
            pass

    asyncio.run(_run())
    assert len(called) >= 1
