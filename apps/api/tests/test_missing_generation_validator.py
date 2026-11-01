from types import SimpleNamespace
from io import BytesIO

import pytest

from creator_service.object_storage import AzureBlobStorageBackend, S3StorageBackend
from creator_service.object_storage_reads import ObjectInfo
from tests.stream_http_support import http_stream


@pytest.mark.parametrize("backend_type", [S3StorageBackend, AzureBlobStorageBackend])
@pytest.mark.parametrize("etag", [None, ""])
@pytest.mark.parametrize("method", ["HEAD", "GET"])
def test_missing_etag_refuses_http_delivery_before_ranged_read(monkeypatch, backend_type, etag, method):
    backend = backend_type.__new__(backend_type)
    backend._prefix = ""
    backend._bucket = "offline"
    calls = []

    def ranged(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(readall=lambda: b"data")

    def s3_ranged(**kwargs):
        calls.append(kwargs)
        return {"Body": BytesIO(b"data")}

    backend._client = SimpleNamespace(get_object=s3_ranged)
    backend._container_client = SimpleNamespace(get_blob_client=lambda _: SimpleNamespace(download_blob=ranged))
    monkeypatch.setattr(backend, "stat", lambda _: ObjectInfo(4, etag))
    with http_stream(backend, "object") as client:
        response = client.request(method, "/content")
    assert response.status_code == 502
    assert calls == []
