import logging
from io import BytesIO

import pytest
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.exceptions import ServiceRequestError
from azure.storage.blob import BlobServiceClient
from botocore.awsrequest import AWSResponse
from requests.structures import CaseInsensitiveDict

from creator_service.object_storage import AzureBlobStorageBackend, S3StorageBackend, StorageReadError
from creator_service.logging_config import JsonFormatter


class AzureResponse(HttpResponse):
    def __init__(self, request, data: bytes, status: int) -> None:
        super().__init__(request, None)
        self.status_code = status
        self.headers = CaseInsensitiveDict({"Content-Length": str(len(data)), "Content-Type": "application/octet-stream",
                        "ETag": '"generation-a"', "Last-Modified": "Mon, 02 Nov 2026 00:00:00 GMT",
                        "Content-Range": "bytes 0-3/4", "x-ms-blob-type": "BlockBlob"})
        self.content_type = "application/octet-stream" if request.method == "GET" else "text/plain"
        self.data = data

    def body(self) -> bytes:
        return self.data

    def stream_download(self, pipeline, **kwargs):
        class Download:
            content_length = len(self.data)

            def __iter__(inner):
                return iter([self.data])
        return Download()


class OfflineAzure(HttpTransport):
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.calls = 0

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.calls += 1
        if self.fail:
            raise ServiceRequestError("offline transport unavailable")
        return AzureResponse(request, b"data", 206 if request.method == "GET" else 200)


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("operation", ["stat", "read", "error"])
def test_real_azure_pipeline_never_logs_storage_identifiers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, level: int, operation: str,
) -> None:
    # Given the real SDK pipeline with an offline transport, even explicitly enabled logging.
    transport = OfflineAzure(operation == "error")
    original = BlobServiceClient.from_connection_string
    connection = "DefaultEndpointsProtocol=https;AccountName=accountmarker;AccountKey=b2ZmbGluZQ==;EndpointSuffix=core.windows.net"
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection)
    monkeypatch.setenv("AZURE_STORAGE_CONTAINER", "containermarker")
    monkeypatch.setenv("AZURE_STORAGE_PREFIX", "prefixmarker")

    def construct(value, **kwargs):
        return original(value, transport=transport, retry_total=0, **{**kwargs, "logging_enable": True})

    monkeypatch.setattr(BlobServiceClient, "from_connection_string", construct)
    caplog.set_level(level)
    backend = AzureBlobStorageBackend()
    # When stat, ranged read or a transport failure runs through actual policies.
    if operation == "error":
        with pytest.raises(StorageReadError):
            backend.stat("objectmarker.mp4")
    elif operation == "stat":
        assert backend.stat("objectmarker.mp4").size == 4
    else:
        assert backend.read_range("objectmarker.mp4", 0, 4) == b"data"
    # Then neither raw records nor the repository JSON formatter disclose identifiers.
    assert transport.calls > 0
    formatted = "\n".join(JsonFormatter("api").format(record) for record in caplog.records)
    for marker in ("accountmarker", "containermarker", "prefixmarker", "objectmarker"):
        assert marker not in caplog.text + formatted


class S3Raw(BytesIO):
    def stream(self, amt=1024, decode_content=False):
        while block := self.read(amt):
            yield block


@pytest.mark.parametrize("level", [logging.INFO, logging.DEBUG])
@pytest.mark.parametrize("operation", ["stat", "read", "error"])
def test_real_botocore_pipeline_never_logs_storage_identifiers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, level: int, operation: str,
) -> None:
    # Given real botocore serialization/signing with only HTTP delivery replaced.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "offline")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline")
    monkeypatch.setenv("AWS_S3_BUCKET", "bucketmarker")
    monkeypatch.setenv("AWS_S3_PREFIX", "prefixmarker")
    caplog.set_level(level)
    backend = S3StorageBackend()
    calls = []

    def send(request):
        calls.append(request.url)
        status = 403 if operation == "error" else 200
        body = b"<Error><Code>AccessDenied</Code></Error>" if status == 403 else b"data"
        wire_body = b"" if operation == "stat" else body
        return AWSResponse(request.url, status, {"content-length": str(len(body)), "etag": '"a"'}, S3Raw(wire_body))

    monkeypatch.setattr(backend._client._endpoint.http_session, "send", send)
    # When the complete SDK pipeline handles a read or rejection.
    if operation == "error":
        with pytest.raises(StorageReadError):
            backend.stat("objectmarker.mp4")
    elif operation == "stat":
        assert backend.stat("objectmarker.mp4").size == 4
    else:
        assert backend.read_range("objectmarker.mp4", 0, 4) == b"data"
    # Then INFO and DEBUG records omit storage identifiers.
    assert calls
    formatted = "\n".join(JsonFormatter("api").format(record) for record in caplog.records)
    for marker in ("bucketmarker", "prefixmarker", "objectmarker"):
        assert marker not in caplog.text + formatted
