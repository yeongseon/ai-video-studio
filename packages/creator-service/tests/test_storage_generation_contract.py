from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from azure.core import MatchConditions

from creator_service.object_storage import S3StorageBackend, StorageReadError


def test_s3_session_binds_etag_and_version_on_every_range(monkeypatch) -> None:
    # Given generation metadata supplied by the real S3 protocol.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "offline")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline")
    monkeypatch.setenv("AWS_S3_BUCKET", "bucket")
    monkeypatch.setenv("AWS_S3_PREFIX", "prefix")
    backend = S3StorageBackend()
    with Stubber(backend._client) as stub:
        stub.add_response("head_object", {"ContentLength": 8, "ETag": '"a"', "VersionId": "v1"},
                          {"Bucket": "bucket", "Key": "prefix/object"})
        for offset, data in ((0, b"aaaa"), (4, b"bbbb")):
            stub.add_response("get_object", {"Body": StreamingBody(BytesIO(data), 4)},
                              {"Bucket": "bucket", "Key": "prefix/object", "Range": f"bytes={offset}-{offset+3}",
                               "IfMatch": '"a"', "VersionId": "v1"})
        # When two bounded ranges are consumed from a session.
        reader = backend.open_read("object")
        try:
            assert reader.info.etag == '"a"' and reader.info.version_id == "v1"
            assert reader.read_range(0, 4) == b"aaaa"
            assert reader.read_range(4, 4) == b"bbbb"
        finally:
            reader.close()


def test_s3_generation_change_is_generic(monkeypatch) -> None:
    # Given a replacement rejected by IfMatch.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "offline")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline")
    monkeypatch.setenv("AWS_S3_BUCKET", "bucket")
    backend = S3StorageBackend()
    with Stubber(backend._client) as stub:
        stub.add_response("head_object", {"ContentLength": 4, "ETag": '"a"'})
        stub.add_client_error("get_object", "PreconditionFailed", "secret-generation", 412)
        reader = backend.open_read("object")
        # When the cloud refuses the old generation.
        with pytest.raises(StorageReadError) as error:
            reader.read_range(0, 4)
        assert "secret-generation" not in str(error.value)


def test_azure_session_binds_etag_match_condition_on_every_range(monkeypatch) -> None:
    from azure.storage.blob import BlobServiceClient
    from creator_service.object_storage import AzureBlobStorageBackend

    # Given real adapter code and a fake SDK client capturing conditional reads.
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(readall=lambda: b"aaaa")

    blob = SimpleNamespace(get_blob_properties=lambda: SimpleNamespace(size=8, etag='"a"'), download_blob=download)
    client = SimpleNamespace(get_container_client=lambda _: SimpleNamespace(get_blob_client=lambda _: blob))
    monkeypatch.setattr(BlobServiceClient, "from_connection_string", lambda *args, **kwargs: client)
    monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", "offline")
    backend = AzureBlobStorageBackend()
    # When every range is read from the generation captured at stat.
    reader = backend.open_read("object")
    try:
        reader.read_range(0, 4)
        reader.read_range(4, 4)
    finally:
        reader.close()
    # Then neither chunk can fetch a replacement generation.
    assert [call["offset"] for call in calls] == [0, 4]
    assert all(call["etag"] == '"a"' and call["match_condition"] == MatchConditions.IfNotModified for call in calls)
