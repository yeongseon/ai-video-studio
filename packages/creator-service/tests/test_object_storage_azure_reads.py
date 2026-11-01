from types import SimpleNamespace

import pytest

from creator_service.object_storage import AzureBlobStorageBackend


class BlobFake:
    """Fake captures actual SDK request arguments, not arbitrary success."""

    def __init__(self) -> None:
        self.requests: list[dict[str, int]] = []
        self.failure: Exception | None = None

    def get_blob_properties(self):
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(size=10, etag='"generation-a"')

    def download_blob(self, **kwargs: int):
        self.requests.append(kwargs)
        if self.failure is not None:
            raise self.failure
        data = b"0123456789"[kwargs["offset"]:kwargs["offset"] + kwargs["length"]]
        return SimpleNamespace(readall=lambda: data)


@pytest.fixture
def azure(monkeypatch: pytest.MonkeyPatch) -> tuple[AzureBlobStorageBackend, BlobFake, list[str]]:
    # Only client construction is injected; adapter operations remain real.
    from azure.storage.blob import BlobServiceClient

    blob = BlobFake()
    names: list[str] = []

    def get_blob(name: str) -> BlobFake:
        names.append(name)
        return blob

    client = SimpleNamespace(get_container_client=lambda _: SimpleNamespace(get_blob_client=get_blob))
    monkeypatch.setattr(BlobServiceClient, "from_connection_string", lambda *args, **kwargs: client)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "offline-test")
    monkeypatch.setenv("AZURE_STORAGE_PREFIX", "configured-prefix")
    return AzureBlobStorageBackend(), blob, names


def test_azure_uses_bounded_offset_length_and_prefix(azure) -> None:
    # Given an injected SDK client with known bytes.
    backend, blob, names = azure
    # When metadata and a bounded range are read.
    info = backend.stat("owned.mp4")
    data = backend.read_range("owned.mp4", 2, 4)
    # Then offset/length and concurrency constrain the SDK's initial download.
    assert info.size == 10
    assert data == b"2345"
    assert names == ["configured-prefix/owned.mp4", "configured-prefix/owned.mp4"]
    assert blob.requests[0]["offset"] == 2
    assert blob.requests[0]["length"] == 4
    assert blob.requests[0]["max_concurrency"] == 1


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize("missing", [True, False])
def test_azure_normalizes_missing_and_auth(azure, operation: str, missing: bool) -> None:
    from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
    from creator_service.object_storage import StorageObjectMissing, StorageReadError

    # Given real SDK exceptions behind a fake client.
    backend, blob, _ = azure
    blob.failure = ResourceNotFoundError("secret-key") if missing else ClientAuthenticationError("secret-key")
    # When a read crosses the provider boundary.
    with pytest.raises(StorageObjectMissing if missing else StorageReadError) as failure:
        if operation == "stat":
            backend.stat("owned.mp4")
        else:
            backend.read_range("owned.mp4", 0, 4)
    # Then errors have stable, generic text.
    assert "secret-key" not in str(failure.value)


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_azure_timeout_is_distinct(azure, operation: str) -> None:
    from creator_service.object_storage import StorageReadError

    # Given a transport timeout.
    backend, blob, _ = azure
    blob.failure = TimeoutError("secret-key")
    # When crossing the adapter boundary.
    with pytest.raises(StorageReadError) as failure:
        if operation == "stat":
            backend.stat("owned.mp4")
        else:
            backend.read_range("owned.mp4", 0, 4)
    # Then callers can select generic timeout semantics.
    assert failure.value.kind == "timeout"
