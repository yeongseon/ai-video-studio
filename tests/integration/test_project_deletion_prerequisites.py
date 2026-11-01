import pytest

import project_deletion_support as support


@pytest.mark.asyncio
async def test_missing_database_fails_when_running_on_github_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRST_SHORT_TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fixture = support.deletion_database.__wrapped__(monkeypatch)
    with pytest.raises(pytest.fail.Exception, match="PostgreSQL"):
        await anext(fixture)


@pytest.mark.asyncio
async def test_missing_database_skips_when_running_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIRST_SHORT_TEST_DATABASE_URL", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fixture = support.deletion_database.__wrapped__(monkeypatch)
    with pytest.raises(pytest.skip.Exception, match="PostgreSQL"):
        await anext(fixture)
