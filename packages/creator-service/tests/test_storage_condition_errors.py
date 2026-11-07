import pytest
from azure.core.exceptions import HttpResponseError
from botocore.exceptions import ClientError

from creator_service.object_storage_reads import azure_read, s3_read, StorageReadError


@pytest.mark.parametrize("provider", ["s3", "azure"])
def test_sdk_condition_failure_is_classified_without_provider_text(provider: str) -> None:
    # Given the SDK's real conditional-failure exception shape.
    def fail_s3(byte_range):
        raise ClientError({"Error": {"Code": "PreconditionFailed", "Message": "private-marker"},
                           "ResponseMetadata": {"HTTPStatusCode": 412}}, "GetObject")

    def fail_azure(start, count):
        error = HttpResponseError("private-marker")
        error.status_code = 412
        error.error_code = "ConditionNotMet"
        raise error

    # When normalization handles a failed generation condition.
    with pytest.raises(StorageReadError) as error:
        if provider == "s3":
            s3_read(fail_s3, 0, 4)
        else:
            azure_read(fail_azure, 0, 4)
    # Then generation changes stay distinguishable and private.
    assert error.value.kind == "generation_changed"
    assert "private-marker" not in str(error.value)
