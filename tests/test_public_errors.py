"""Public errors must remain useful without leaking diagnostic strings."""

import json

from garmin_connect_mcp.client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminMutationInProgressError,
    GarminMutationOutcomeUnknownError,
)
from garmin_connect_mcp.response_builder import ResponseBuilder


def test_internal_error_omits_raw_secret_path_url_and_log_message(caplog):
    raw = OSError(
        "CANARY_SECRET at C:\\Users\\Alice\\.garminconnect\\tokens.json "
        "https://example.test/api?token=CANARY_SECRET"
    )

    response = ResponseBuilder.build_exception_response(raw)
    payload = json.loads(response)

    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in response
    assert "Alice" not in response
    assert "CANARY" not in caplog.text
    assert "Alice" not in caplog.text


def test_original_upstream_exception_is_not_exposed():
    error = GarminAPIError(
        "Garmin Connect could not complete the request. Try again later.",
        original_error=RuntimeError("CANARY_SECRET upstream body"),
    )
    response = ResponseBuilder.build_exception_response(error)
    payload = json.loads(response)

    assert payload["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert payload["error"]["message"] == (
        "Garmin Connect could not complete the request. Try again later."
    )
    assert "CANARY" not in response


def test_generic_garmin_error_message_is_never_treated_as_public_text():
    response = ResponseBuilder.build_exception_response(
        GarminAPIError("CANARY_SECRET at C:\\Users\\Alice\\tokens.json")
    )
    payload = json.loads(response)

    assert payload["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert payload["error"]["message"] == (
        "Garmin Connect could not complete the request. Try again later."
    )
    assert "CANARY" not in response
    assert "Alice" not in response


def test_authentication_error_uses_stable_public_message():
    response = ResponseBuilder.build_exception_response(
        GarminAuthenticationError("CANARY_SECRET token loader detail")
    )
    payload = json.loads(response)

    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert payload["error"]["message"] == (
        "Garmin authentication is required. Run 'garmin-connect-mcp auth'."
    )
    assert "CANARY" not in response


def test_bounded_validation_error_has_stable_code_without_request_id():
    payload = json.loads(
        ResponseBuilder.build_error_response("device_id must be positive", "invalid_parameters")
    )
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "request_id" not in payload["error"]


def test_mutation_errors_use_documented_upstream_code_with_safe_retry_guidance():
    unknown = json.loads(
        ResponseBuilder.build_exception_response(
            GarminMutationOutcomeUnknownError("CANARY_SECRET raw mutation detail")
        )
    )
    in_progress = json.loads(
        ResponseBuilder.build_exception_response(
            GarminMutationInProgressError("CANARY_SECRET raw lock detail")
        )
    )

    assert unknown["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert "do not use a new idempotency_key" in unknown["error"]["message"]
    assert in_progress["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert "same idempotency_key" in in_progress["error"]["message"]
    assert "CANARY" not in json.dumps([unknown, in_progress])
