"""Delete persisted run objects using their recorded storage location."""

import os
from pathlib import Path

from creator_service.object_storage import (
    ArtifactStorageBackend,
    AzureBlobStorageBackend,
    LocalStorageBackend,
    S3StorageBackend,
    StorageDeletionError,
)


def delete_run_object(
    key: str, provider: str | None, configured_backend: ArtifactStorageBackend,
) -> None:
    configured_provider = os.getenv("STORAGE_BACKEND", "local")
    effective_provider = provider or configured_provider
    backend = configured_backend
    if effective_provider != configured_provider:
        match effective_provider:
            case "local":
                backend = LocalStorageBackend()
            case "s3":
                backend = S3StorageBackend()
            case "azure_blob":
                backend = AzureBlobStorageBackend()
            case _:
                raise StorageDeletionError(key)
    if effective_provider == "local" and Path(key).is_absolute():
        root = Path(os.getenv("ARTIFACT_ROOT", "data/artifacts")).resolve()
        local_key = str(Path(key).resolve().relative_to(root))
        backend.delete(local_key)
        return
    backend.delete(key)
