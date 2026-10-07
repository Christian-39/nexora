"""Upload provider diagnostics remain correlated and content-free."""

from apps.media.services import storage_failure_context


def test_storage_failure_context_extracts_only_safe_provider_status_and_ids():
    error = RuntimeError("object key, body data and credentials must never be logged")
    error.response = {
        "Error": {
            "Code": "ServiceUnavailable",
            "Message": "private bucket/object path and secret response text",
        },
        "ResponseMetadata": {
            "HTTPStatusCode": 503,
            "RequestId": "provider-request-123",
            "HTTPHeaders": {"authorization": "secret-token"},
        },
    }

    context = storage_failure_context(error)

    assert context == {
        "storage_http_status": 503,
        "storage_error_code": "ServiceUnavailable",
        "storage_request_id": "provider-request-123",
    }
    assert "Message" not in context
    assert "secret-token" not in repr(context)


def test_storage_failure_context_omits_malformed_provider_details():
    error = RuntimeError("do not include this message")
    error.response = {
        "Error": {"Code": "Service Unavailable"},
        "ResponseMetadata": {"HTTPStatusCode": "not-a-status", "RequestId": "unsafe request id"},
    }

    assert storage_failure_context(error) == {}
