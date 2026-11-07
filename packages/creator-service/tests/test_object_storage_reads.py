from io import BytesIO
from pathlib import Path

import pytest

from creator_service.object_storage import LocalStorageBackend, S3StorageBackend


def test_local_stat_and_bounded_range(tmp_path: Path) -> None:
    # Given a stored local representation.
    backend = LocalStorageBackend(str(tmp_path))
    backend.upload("owned/video.mp4", b"0123456789")
    # When stat and a bounded read are requested.
    info = backend.stat("owned/video.mp4")
    data = backend.read_range("owned/video.mp4", 2, 4)
    # Then they expose the size and exactly the selected bytes.
    assert info.size == 10
    assert data == b"2345"


@pytest.mark.parametrize("operation", ["stat", "read"])
@pytest.mark.parametrize("key", ["../outside", "/etc/passwd", "escape", "dir/escape"])
def test_local_read_confinement(tmp_path: Path, operation: str, key: str) -> None:
    from creator_service.object_storage import StorageObjectMissing

    # Given file and directory symlinks escaping the root.
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(b"secret")
    (tmp_path / "escape").symlink_to(outside)
    (tmp_path / "dir").symlink_to(tmp_path.parent, target_is_directory=True)
    backend = LocalStorageBackend(str(tmp_path))
    # When either read operation crosses the local boundary.
    with pytest.raises(StorageObjectMissing):
        if operation == "stat":
            backend.stat(key)
        else:
            backend.read_range(key, 0, 6)


@pytest.mark.parametrize("length", [0, -1, 65537])
def test_read_contract_rejects_unbounded_requests(tmp_path: Path, length: int) -> None:
    # Given a local adapter and invalid read size.
    backend = LocalStorageBackend(str(tmp_path))
    # When a caller violates the bounded contract.
    with pytest.raises(ValueError):
        backend.read_range("object", 0, length)


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> S3StorageBackend:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "offline-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline-test")
    monkeypatch.setenv("AWS_S3_BUCKET", "configured-bucket")
    monkeypatch.setenv("AWS_S3_PREFIX", "configured-prefix")
    return S3StorageBackend()


def test_s3_stat_and_range_use_configured_bucket_prefix_and_close(s3: S3StorageBackend) -> None:
    from botocore.response import StreamingBody
    from botocore.stub import Stubber

    # Given SDK responses and an exact expected bucket/prefix/range.
    raw = BytesIO(b"2345")
    body = StreamingBody(raw, 4)
    with Stubber(s3._client) as stub:
        stub.add_response("head_object", {"ContentLength": 10},
                          {"Bucket": "configured-bucket", "Key": "configured-prefix/owned.mp4"})
        stub.add_response("get_object", {"Body": body, "ContentLength": 4},
                          {"Bucket": "configured-bucket", "Key": "configured-prefix/owned.mp4", "Range": "bytes=2-5"})
        # When the adapter reads metadata and bytes.
        info = s3.stat("owned.mp4")
        result = s3.read_range("owned.mp4", 2, 4)
    # Then it never downloads the full object and releases the response.
    assert info.size == 10
    assert result == b"2345"
    assert raw.closed


@pytest.mark.parametrize("operation", ["head_object", "get_object"])
@pytest.mark.parametrize("code,status,missing", [("NoSuchKey", 404, True), ("404", 404, True),
                                                  ("AccessDenied", 403, False), ("NoSuchBucket", 404, False)])
def test_s3_normalizes_missing_and_auth_errors(
    s3: S3StorageBackend, operation: str, code: str, status: int, missing: bool,
) -> None:
    from botocore.stub import Stubber
    from creator_service.object_storage import StorageObjectMissing, StorageReadError

    # Given a provider exception containing a sensitive key.
    with Stubber(s3._client) as stub:
        stub.add_client_error(operation, service_error_code=code, service_message="secret-key",
                              http_status_code=status)
        # When the SDK boundary fails.
        with pytest.raises(StorageObjectMissing if missing else StorageReadError) as failure:
            if operation == "head_object":
                s3.stat("owned.mp4")
            else:
                s3.read_range("owned.mp4", 0, 4)
    # Then normalized exceptions contain no provider message.
    assert "secret-key" not in str(failure.value)


def test_s3_closes_response_when_read_fails(s3: S3StorageBackend) -> None:
    from botocore.response import StreamingBody
    from botocore.stub import Stubber
    from creator_service.object_storage import StorageReadError

    # Given a response that fails while being consumed.
    class BrokenBody(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("secret-key")

    raw = BrokenBody(b"data")
    with Stubber(s3._client) as stub:
        stub.add_response("get_object", {"Body": StreamingBody(raw, 4)})
        # When reading fails.
        with pytest.raises(StorageReadError):
            s3.read_range("owned.mp4", 0, 4)
    # Then the failed provider response is released too.
    assert raw.closed


@pytest.mark.parametrize("operation", ["stat", "read"])
def test_s3_read_timeout_is_distinct(s3: S3StorageBackend, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    from botocore.exceptions import ReadTimeoutError
    from creator_service.object_storage import StorageReadError

    # Given a real SDK timeout raised without a network connection.
    def fail(**kwargs):
        raise ReadTimeoutError(endpoint_url="https://secret-key")

    monkeypatch.setattr(s3._client, "head_object" if operation == "stat" else "get_object", fail)
    # When the adapter reads.
    with pytest.raises(StorageReadError) as failure:
        if operation == "stat":
            s3.stat("owned.mp4")
        else:
            s3.read_range("owned.mp4", 0, 4)
    # Then timeout remains distinguishable without endpoint text.
    assert failure.value.kind == "timeout"
    assert "secret-key" not in str(failure.value)
