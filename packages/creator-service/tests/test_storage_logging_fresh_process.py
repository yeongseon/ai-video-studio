import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
@pytest.mark.parametrize("provider", ["azure", "s3"])
@pytest.mark.parametrize("operation", ["stat", "read", "error"])
def test_first_sdk_operation_in_clean_interpreter_has_no_identifier_logs(level, provider, operation) -> None:
    # Given a clean interpreter with the real offline SDK fixtures and every logging sink captured.
    code = '''
import io, logging, warnings
from unittest.mock import patch
from test_storage_pipeline_logging import OfflineAzure, S3Raw
from creator_service.logging_config import JsonFormatter
from creator_service.object_storage import AzureBlobStorageBackend, S3StorageBackend, StorageReadError
from azure.storage.blob import BlobServiceClient
from botocore.awsrequest import AWSResponse
stream=io.StringIO()
handler=logging.StreamHandler(stream)
handler.setFormatter(JsonFormatter("probe"))
logging.getLogger().handlers=[handler]
logging.getLogger().setLevel(LEVEL)
logging.lastResort=handler
logging.captureWarnings(True)
if PROVIDER=="azure":
    original=BlobServiceClient.from_connection_string
    with patch.object(BlobServiceClient,"from_connection_string",lambda value,**kw: original(value,transport=OfflineAzure(OPERATION=="error"),retry_total=0,**{**kw,"logging_enable":True})):
        backend=AzureBlobStorageBackend()
else:
    backend=S3StorageBackend()
    def send(request):
        status=403 if OPERATION=="error" else 200
        body=b"<Error><Code>AccessDenied</Code></Error>" if status==403 else b"data"
        return AWSResponse(request.url,status,{"content-length":str(len(body)),"etag":'"a"'},S3Raw(b"" if OPERATION=="stat" else body))
    backend._client._endpoint.http_session.send=send
try:
    if OPERATION=="read":
        assert backend.read_range("objectmarker.mp4",0,4)==b"data"
    else:
        backend.stat("objectmarker.mp4")
except StorageReadError:
    assert OPERATION=="error"
for marker in ("accountmarker","containermarker","bucketmarker","prefixmarker","objectmarker"):
    assert marker not in stream.getvalue(), stream.getvalue()
print("clean")
'''
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent),
           "AWS_ACCESS_KEY_ID": "offline", "AWS_SECRET_ACCESS_KEY": "offline",
           "AWS_S3_BUCKET": "bucketmarker", "AWS_S3_PREFIX": "prefixmarker",
           "AZURE_STORAGE_CONTAINER": "containermarker", "AZURE_STORAGE_PREFIX": "prefixmarker",
           "AZURE_STORAGE_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountName=accountmarker;AccountKey=b2ZmbGluZQ==;EndpointSuffix=core.windows.net"}
    # When the first real pipeline operation runs without warmed logger state.
    result = subprocess.run([sys.executable, "-c", f"LEVEL={level!r}\nPROVIDER={provider!r}\nOPERATION={operation!r}\n" + code],
                            env=env, capture_output=True, text=True, timeout=20)
    # Then root handlers, lastResort, warnings and subprocess stderr disclose no storage marker.
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "clean"
    assert not result.stderr
