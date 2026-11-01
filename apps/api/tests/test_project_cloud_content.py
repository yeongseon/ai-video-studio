"""Security boundaries must reject before *any* preview storage call."""

from pathlib import Path

from httpx import AsyncClient
import pytest

from tests.project_preview_support import PreviewFixture, content_url, preview as preview

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("provider", ["s3", "azure_blob"])
@pytest.mark.parametrize("mime", ["image/png", "video/mp4"])
async def test_project_cloud_content_serves_owned_bytes_without_local_fallback(
    client: AsyncClient, preview: PreviewFixture, provider: str, mime: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given owned remote bytes, with different bytes at the same local key.
    preview.backend.provider = provider
    monkeypatch.setenv("STORAGE_BACKEND", provider)
    asset = await preview.asset(provider=provider, mime=mime)
    # When requested by project identity, without following redirects.
    response = await client.get(content_url(asset), follow_redirects=False)
    # Then delivery stays same-origin with the stored MIME, not adapter metadata.
    assert response.status_code == 200, response.text
    assert response.content == preview.backend.data
    assert response.headers["content-type"] == mime
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert "location" not in response.headers
    assert "content-disposition" not in response.headers
    assert preview.backend.calls > 0


@pytest.mark.parametrize("scenario", ["foreign-project", "foreign-asset", "other-project", "missing"])
async def test_project_preview_denies_before_storage(
    client: AsyncClient, preview: PreviewFixture, scenario: str,
) -> None:
    # Given foreign, incorrectly associated, or absent identities.
    asset = await preview.asset(workspace=8 if scenario == "foreign-asset" else 7)
    project = 99 if scenario == "foreign-project" else 43 if scenario == "other-project" else 42
    url = content_url(99999 if scenario == "missing" else asset, project)
    # When an authenticated caller probes them.
    response = await client.get(url)
    # Then no existence oracle or storage side effect exists.
    assert response.status_code == 404
    assert preview.backend.calls == 0


async def test_project_preview_requires_authentication(client: AsyncClient, preview: PreviewFixture) -> None:
    # Given a valid identity but no credentials.
    asset = await preview.asset()
    client.headers.clear()
    # When requested anonymously.
    response = await client.get(content_url(asset))
    # Then the backend is never contacted.
    assert response.status_code == 401
    assert preview.backend.calls == 0


@pytest.mark.parametrize("key", ["workspaces/8/assets/private.mp4", "../private.mp4", "/private.mp4", "a\\private.mp4"])
async def test_remote_keys_cannot_select_foreign_namespace(
    client: AsyncClient, preview: PreviewFixture, key: str,
) -> None:
    # Given an owned row with a foreign or unsafe storage key.
    asset = await preview.asset(key=key)
    # When its project identity is requested.
    response = await client.get(content_url(asset))
    # Then namespace confusion never reaches storage.
    assert response.status_code == 404
    assert preview.backend.calls == 0


async def test_provider_mismatch_is_409_without_storage(client: AsyncClient, preview: PreviewFixture) -> None:
    # Given historical metadata that differs from the configured singleton.
    asset = await preview.asset(provider="azure_blob")
    # When its identity is requested.
    response = await client.get(content_url(asset))
    # Then it cannot accidentally serve a colliding key from another provider.
    assert response.status_code == 409
    assert preview.backend.calls == 0


async def test_singleton_provider_mismatch_does_not_read(
    client: AsyncClient, preview: PreviewFixture,
) -> None:
    # Given environment changed after a singleton was initialized for another provider.
    asset = await preview.asset()
    preview.backend.provider = "azure_blob"
    # When requesting an s3 asset under s3 configuration.
    response = await client.get(content_url(asset))
    # Then the actual backend identity is checked, not merely current environment.
    assert response.status_code == 409
    assert preview.backend.calls == 0


async def test_backend_initialization_failure_is_generic(
    client: AsyncClient, preview: PreviewFixture, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from botocore.exceptions import NoCredentialsError
    from shorts_api.routes import creator_timeline

    # Given credentials unavailable during lazy SDK initialization.
    asset = await preview.asset()

    def fail():
        raise NoCredentialsError()

    monkeypatch.setattr(creator_timeline, "get_storage_backend", fail)
    # When the authenticated route initializes storage.
    response = await client.get(content_url(asset))
    # Then deployment errors fail closed without leaking SDK diagnostics.
    assert response.status_code == 502
    assert "credentials" not in response.text.lower()
    assert "private.mp4" not in caplog.text


@pytest.mark.parametrize("mime", ["text/html", "image/svg+xml", "audio/wav"])
async def test_preview_mime_allowlist_precedes_storage(
    client: AsyncClient, preview: PreviewFixture, mime: str,
) -> None:
    # Given a stored but unpreviewable MIME.
    asset = await preview.asset(mime=mime)
    # When requested as inline content.
    response = await client.get(content_url(asset))
    # Then no active content or out-of-scope audio can be delivered.
    assert response.status_code == 415
    assert preview.backend.calls == 0


@pytest.mark.parametrize("key", ["../secret", "/etc/passwd", "workspaces\\secret", "escape.mp4"])
async def test_local_preview_confines_keys(
    client: AsyncClient, preview: PreviewFixture, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, key: str,
) -> None:
    # Given traversal or a symlink to bytes outside the root.
    outside = tmp_path.parent / f"{tmp_path.name}-secret"
    outside.write_bytes(b"private-outside")
    (tmp_path / "escape.mp4").symlink_to(outside)
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    preview.backend.provider = "local"
    from creator_service.object_storage import LocalStorageBackend

    monkeypatch.setattr("creator_service.object_storage._storage_backend", LocalStorageBackend(str(tmp_path)))
    asset = await preview.asset(provider="local", key=key)
    # When requested through the owned identity.
    response = await client.get(content_url(asset))
    # Then local confinement still rejects it.
    assert response.status_code == 404
    assert b"private-outside" not in response.content


@pytest.mark.parametrize("kind,status", [("missing", 404), ("timeout", 503), ("auth", 502), ("config", 502)])
async def test_remote_failures_are_generic(
    client: AsyncClient, preview: PreviewFixture, caplog: pytest.LogCaptureFixture, kind: str, status: int,
) -> None:
    from creator_service.object_storage import StorageReadError, StorageObjectMissing

    # Given provider exceptions containing secrets and an authorized asset.
    asset = await preview.asset()
    preview.backend.failure = StorageObjectMissing() if kind == "missing" else StorageReadError(kind)
    preview.backend.failure.__cause__ = RuntimeError("private.mp4 SAS=secret-provider-text")
    # When reading the object fails before headers are committed.
    response = await client.get(content_url(asset))
    # Then errors and logs disclose neither key nor provider diagnostics.
    assert response.status_code == status
    assert "private.mp4" not in response.text + caplog.text
    assert "secret-provider-text" not in response.text + caplog.text
    assert preview.backend.calls == 1


async def test_gcs_is_explicitly_unsupported(
    client: AsyncClient, preview: PreviewFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given matching but unimplemented provider configuration.
    monkeypatch.setenv("STORAGE_BACKEND", "gcs")
    preview.backend.provider = "gcs"
    asset = await preview.asset(provider="gcs")
    # When requested through the identity route.
    response = await client.get(content_url(asset))
    # Then unsupported is not an internal server error or fallback.
    assert response.status_code == 409
    assert preview.backend.calls == 0
