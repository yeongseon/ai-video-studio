"""Object storage abstraction for artifact files.

Selects backend based on STORAGE_BACKEND env var:
- 'local' (default): LocalStorageBackend using ARTIFACT_ROOT
- 's3': S3StorageBackend (requires boto3)
- 'azure_blob': AzureBlobStorageBackend (requires azure-storage-blob)
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Final, Protocol, cast

from .object_storage_reads import (
    ObjectInfo as ObjectInfo,
    StorageObjectMissing as StorageObjectMissing,
    StorageReadError as StorageReadError,
    azure_read, azure_stat, local_read, local_stat, s3_read, s3_stat, validate_key,
)
from .storage_read_session import LocalReader, ObjectReader, RemoteReader

logger = logging.getLogger(__name__)
_STREAM_CHUNK_SIZE = 64 * 1024
PROVIDER_CONNECT_TIMEOUT_SECONDS: Final = 30
PROVIDER_READ_TIMEOUT_SECONDS: Final = 600


def _normalize_content_type(content_type: str) -> str:
    return content_type or "application/octet-stream"


def _spool_and_hash_stream(data: BinaryIO) -> tuple[BinaryIO, int, str]:
    size = 0
    md5 = hashlib.md5(usedforsecurity=False)
    spooled = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
    while chunk := data.read(_STREAM_CHUNK_SIZE):
        spooled.write(chunk)
        md5.update(chunk)
        size += len(chunk)
    spooled.seek(0)
    return cast(BinaryIO, spooled), size, md5.hexdigest()


class StorageCredentialMissingError(RuntimeError):
    """Raised when storage credentials are missing or insufficient.

    This is a critical error that must not be silently ignored.
    Unsigned URLs are not acceptable in production.
    """

    pass


@dataclass(frozen=True, slots=True)
class StorageDeletionError(RuntimeError):
    key: str

    def __str__(self) -> str:
        return f"object storage deletion failed for {self.key}"


@dataclass
class StorageResult:
    """Result of an upload operation."""

    key: str
    size_bytes: int
    content_type: str
    checksum: str  # MD5 hex digest
    storage_provider: str  # 'local', 's3', 'azure_blob'


class ArtifactStorageBackend(Protocol):
    """Interface for artifact file storage."""

    @property
    def provider(self) -> str: ...

    def upload(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageResult:
        """Upload artifact data and return storage metadata."""
        ...

    def download_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a URL for downloading the artifact.

        For local: returns a file:// path or relative path.
        For cloud: returns a presigned/SAS URL.
        """
        ...

    def download_bytes(self, key: str) -> bytes:
        """Download artifact data as bytes."""
        ...

    def delete(self, key: str) -> bool:
        """Delete artifact. Returns True if deleted, False if not found."""
        ...

    def exists(self, key: str) -> bool:
        """Check if artifact exists."""
        ...

    def stat(self, key: str) -> ObjectInfo:
        """Read object size without downloading content; missing is normalized."""
        ...

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        """Read 1..65536 bytes, releasing the response before returning."""
        ...

    def open_read(self, key: str) -> ObjectReader:
        """Acquire metadata and an owned generation-consistent read session."""
        ...


class LocalStorageBackend:
    """Local filesystem storage. Default for development."""

    def __init__(self, root: str | None = None) -> None:
        self._root = Path(root or os.getenv("ARTIFACT_ROOT", "data/artifacts"))

    provider = "local"

    def _safe_key(self, key: str) -> str:
        """Validate key to prevent path traversal attacks."""
        resolved = (self._root / key).resolve()
        root_resolved = self._root.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise ValueError(f"Invalid key (path traversal): {key}")
        return key

    def upload(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageResult:
        safe_key = self._safe_key(key)
        normalized_content_type = _normalize_content_type(content_type)
        path = self._root / safe_key
        path.parent.mkdir(parents=True, exist_ok=True)

        md5 = hashlib.md5(usedforsecurity=False)
        if isinstance(data, bytes):
            path.write_bytes(data)
            md5.update(data)
            size = len(data)
        else:
            # Check if source file is the same as destination to avoid self-truncation
            source_path = Path(data.name).resolve() if hasattr(data, 'name') else None
            if source_path and source_path == path.resolve():
                # Source and destination are the same file — just compute checksum
                data.seek(0)
                while chunk := data.read(8192):
                    md5.update(chunk)
                size = path.stat().st_size
            else:
                with open(path, "wb") as file_obj:
                    while chunk := data.read(8192):
                        file_obj.write(chunk)
                        md5.update(chunk)
                size = path.stat().st_size

        return StorageResult(
            key=key,
            size_bytes=size,
            content_type=normalized_content_type,
            checksum=md5.hexdigest(),
            storage_provider="local",
        )

    def download_url(self, key: str, expires_in: int = 3600) -> str:
        _ = expires_in
        safe_key = self._safe_key(key)
        return f"/api/artifacts/files/{safe_key}"

    def download_bytes(self, key: str) -> bytes:
        return (self._root / self._safe_key(key)).read_bytes()

    def delete(self, key: str) -> bool:
        path = self._root / self._safe_key(key)
        try:
            if path.is_file():
                path.unlink()
                return True
            return False
        except FileNotFoundError:
            return False
        except OSError as error:
            raise StorageDeletionError(key=key) from error

    def exists(self, key: str) -> bool:
        return (self._root / self._safe_key(key)).is_file()

    def stat(self, key: str) -> ObjectInfo:
        return local_stat(self._root, key)

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        return local_read(self._root, key, offset, length)

    def open_read(self, key: str) -> ObjectReader:
        from .object_storage_reads import local_descriptor

        try:
            return LocalReader(local_descriptor(self._root, key))
        except OSError:
            raise StorageObjectMissing() from None


class S3StorageBackend:
    """AWS S3 storage backend.

    Requires boto3. Install: pip install boto3

    Configure via environment:
        AWS_S3_BUCKET: Bucket name
        AWS_S3_REGION: Region (default: us-east-1)
        AWS_S3_PREFIX: Optional key prefix
    """

    def __init__(self) -> None:
        try:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]
        except ImportError as error:
            raise ImportError(
                "S3StorageBackend requires boto3. Install with: pip install boto3"
            ) from error

        self._bucket = os.environ["AWS_S3_BUCKET"]
        self._region = os.getenv("AWS_S3_REGION", "us-east-1")
        self._prefix = os.getenv("AWS_S3_PREFIX", "")
        self._client = boto3.client(
            "s3",
            region_name=self._region,
            config=Config(
                connect_timeout=PROVIDER_CONNECT_TIMEOUT_SECONDS,
                read_timeout=PROVIDER_READ_TIMEOUT_SECONDS,
                retries={"mode": "standard", "total_max_attempts": 3},
                tcp_keepalive=True,
            ),
        )

    provider = "s3"

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def upload(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageResult:
        full_key = self._full_key(key)
        normalized_content_type = _normalize_content_type(content_type)
        if isinstance(data, bytes):
            body = data
            size = len(data)
            md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()
            self._client.put_object(
                Bucket=self._bucket,
                Key=full_key,
                Body=body,
                ContentType=normalized_content_type,
            )
        else:
            body, size, md5 = _spool_and_hash_stream(data)
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=full_key,
                    Body=body,
                    ContentType=normalized_content_type,
                )
            finally:
                body.close()
        return StorageResult(
            key=key,
            size_bytes=size,
            content_type=normalized_content_type,
            checksum=md5,
            storage_provider="s3",
        )

    def download_url(self, key: str, expires_in: int = 3600) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": self._full_key(key)},
            ExpiresIn=expires_in,
        )

    def download_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=self._full_key(key))
        return response["Body"].read()

    def delete(self, key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._full_key(key))
        except Exception as error:  # noqa: BLE001 - provider SDK boundary normalization
            raise StorageDeletionError(key=key) from error
        return True

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def stat(self, key: str) -> ObjectInfo:
        validate_key(key)
        return s3_stat(lambda: self._client.head_object(Bucket=self._bucket, Key=self._full_key(key)))

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        validate_key(key)
        return s3_read(lambda byte_range: self._client.get_object(
            Bucket=self._bucket, Key=self._full_key(key), Range=byte_range,
        ), offset, length)

    def open_read(self, key: str) -> ObjectReader:
        info = self.stat(key)
        if not info.etag:
            raise StorageReadError()

        def read(offset: int, length: int) -> bytes:
            def operation(byte_range: str):
                parameters = {"Bucket": self._bucket, "Key": self._full_key(key),
                              "Range": byte_range, "IfMatch": info.etag}
                if info.version_id is not None:
                    parameters["VersionId"] = info.version_id
                return self._client.get_object(**parameters)
            return s3_read(operation, offset, length)

        return RemoteReader(info, read)


class AzureBlobStorageBackend:
    """Azure Blob Storage backend.

    Requires azure-storage-blob. Install: pip install azure-storage-blob

    Configure via environment:
        AZURE_STORAGE_CONNECTION_STRING: Storage account connection string
        AZURE_STORAGE_CONTAINER: Container name
        AZURE_STORAGE_PREFIX: Optional blob prefix
    """

    def __init__(self) -> None:
        try:
            from azure.storage.blob import (  # type: ignore[import-untyped]
                BlobSasPermissions,
                BlobServiceClient,
                ContentSettings,
                generate_blob_sas,
            )
        except ImportError as error:
            raise ImportError(
                "AzureBlobStorageBackend requires azure-storage-blob. "
                "Install with: pip install azure-storage-blob"
            ) from error

        self._connection_string = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
        self._container = os.getenv("AZURE_STORAGE_CONTAINER", "artifacts")
        self._prefix = os.getenv("AZURE_STORAGE_PREFIX", "")
        self._client = BlobServiceClient.from_connection_string(
            self._connection_string,
            connection_timeout=PROVIDER_CONNECT_TIMEOUT_SECONDS,
            read_timeout=PROVIDER_READ_TIMEOUT_SECONDS,
            logging_enable=False,
        )
        self._container_client = self._client.get_container_client(self._container)
        self._content_settings_cls = ContentSettings
        self._blob_sas_permissions_cls = BlobSasPermissions
        self._generate_blob_sas = generate_blob_sas

    provider = "azure_blob"

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def upload(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str = "application/octet-stream",
    ) -> StorageResult:
        full_key = self._full_key(key)
        normalized_content_type = _normalize_content_type(content_type)
        if isinstance(data, bytes):
            body = data
            size = len(data)
            checksum = hashlib.md5(body, usedforsecurity=False).hexdigest()
            blob_client = self._container_client.get_blob_client(full_key)
            blob_client.upload_blob(
                body,
                overwrite=True,
                content_settings=self._content_settings_cls(content_type=normalized_content_type),
                timeout=PROVIDER_READ_TIMEOUT_SECONDS,
            )
        else:
            body, size, checksum = _spool_and_hash_stream(data)
            blob_client = self._container_client.get_blob_client(full_key)
            try:
                blob_client.upload_blob(
                    body,
                    overwrite=True,
                    content_settings=self._content_settings_cls(
                        content_type=normalized_content_type
                    ),
                    timeout=PROVIDER_READ_TIMEOUT_SECONDS,
                )
            finally:
                body.close()
        return StorageResult(
            key=key,
            size_bytes=size,
            content_type=normalized_content_type,
            checksum=checksum,
            storage_provider="azure_blob",
        )

    def download_url(self, key: str, expires_in: int = 3600) -> str:
        blob_name = self._full_key(key)
        blob_client = self._container_client.get_blob_client(blob_name)
        credential = getattr(self._client, "credential", None)
        account_key = getattr(credential, "account_key", None)
        account_name = getattr(self._client, "account_name", None)

        if account_key and account_name:
            expires_on = datetime.now(timezone.utc) + timedelta(seconds=max(1, expires_in))
            sas_token = self._generate_blob_sas(
                account_name=account_name,
                container_name=self._container,
                blob_name=blob_name,
                account_key=account_key,
                permission=self._blob_sas_permissions_cls(read=True),
                expiry=expires_on,
            )
            return f"{blob_client.url}?{sas_token}"

        # SECURITY: Never fall back to unsigned URLs. Missing credentials = deployment error, not a runtime choice.
        allow_unsigned = os.getenv("STORAGE_ALLOW_UNSIGNED_URLS", "false").lower() == "true"
        if not allow_unsigned:
            error_msg = (
                "Azure Blob Storage credential missing (account_key unavailable). "
                "Cannot generate signed URL. Set STORAGE_ALLOW_UNSIGNED_URLS=true in .env for local development. "
                "In production, this is a deployment configuration error."
            )
            logger.critical(error_msg)
            raise StorageCredentialMissingError(error_msg)

        # Development fallback: unsigned URL allowed via explicit env variable
        logger.warning(
            "Generating unsigned Azure Blob URL for key=%s (STORAGE_ALLOW_UNSIGNED_URLS=true). ",
            blob_name,
        )
        return f"{blob_client.url}?se={max(1, expires_in)}"

    def download_bytes(self, key: str) -> bytes:
        blob_client = self._container_client.get_blob_client(self._full_key(key))
        return blob_client.download_blob().readall()

    def delete(self, key: str) -> bool:
        blob_client = self._container_client.get_blob_client(self._full_key(key))
        try:
            blob_client.delete_blob(delete_snapshots="include")
        except Exception as error:  # noqa: BLE001 - provider SDK boundary normalization
            if type(error).__name__ == "ResourceNotFoundError":
                return False
            raise StorageDeletionError(key=key) from error
        return True

    def exists(self, key: str) -> bool:
        blob_client = self._container_client.get_blob_client(self._full_key(key))
        return bool(blob_client.exists())

    def stat(self, key: str) -> ObjectInfo:
        validate_key(key)
        return azure_stat(lambda: self._container_client.get_blob_client(self._full_key(key)).get_blob_properties())

    def read_range(self, key: str, offset: int, length: int) -> bytes:
        validate_key(key)
        return azure_read(lambda start, count: self._container_client.get_blob_client(self._full_key(key)).download_blob(
            offset=start, length=count, max_concurrency=1,
        ).readall(), offset, length)

    def open_read(self, key: str) -> ObjectReader:
        from azure.core import MatchConditions

        info = self.stat(key)
        if not info.etag:
            raise StorageReadError()

        def read(offset: int, length: int) -> bytes:
            return azure_read(lambda start, count: self._container_client.get_blob_client(self._full_key(key)).download_blob(
                offset=start, length=count, max_concurrency=1, etag=info.etag,
                match_condition=MatchConditions.IfNotModified, logging_enable=False,
            ).readall(), offset, length)

        return RemoteReader(info, read)


class GCSStorageBackend:
    """Google Cloud Storage backend (not yet implemented).

    Requires google-cloud-storage. Install: pip install google-cloud-storage

    Configure via environment:
        GCS_BUCKET: Bucket name
        GCS_PREFIX: Optional key prefix
        GOOGLE_APPLICATION_CREDENTIALS: Path to service account JSON
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "GCS storage backend is not yet implemented. "
            "Use STORAGE_BACKEND=s3 or STORAGE_BACKEND=azure_blob. "
            "To contribute GCS support, implement this class following "
            "the S3StorageBackend pattern."
        )


def create_storage_backend() -> ArtifactStorageBackend:
    backend = os.getenv("STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorageBackend()
    if backend == "s3":
        return S3StorageBackend()
    if backend == "azure_blob":
        return AzureBlobStorageBackend()
    if backend == "gcs":
        return cast(ArtifactStorageBackend, GCSStorageBackend())
    raise ValueError(f"Unknown STORAGE_BACKEND: {backend}")


def _get_storage_backend() -> ArtifactStorageBackend:
    """Lazy singleton — created on first access, not at import time."""
    global _storage_backend  # noqa: PLW0603
    if _storage_backend is None:
        _storage_backend = create_storage_backend()
    return _storage_backend


_storage_backend: ArtifactStorageBackend | None = None


def get_storage_backend() -> ArtifactStorageBackend:
    """Public accessor for the storage backend singleton."""
    return _get_storage_backend()
