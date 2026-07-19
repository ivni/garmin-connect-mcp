"""Stable public errors without raw exception disclosure."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from ..client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminMethodNotAllowedError,
    GarminMethodUnavailableError,
    GarminMutationInProgressError,
    GarminMutationOutcomeUnknownError,
    GarminNotFoundError,
    GarminRateLimitError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublicError:
    """One safe user-visible error."""

    code: str
    error_type: str
    message: str
    request_id: str


def public_error_for_exception(error: Exception) -> PublicError:
    """Classify an exception without exposing its raw string representation."""
    request_id = f"err-{secrets.token_hex(6)}"
    logger.error(
        "Garmin MCP request failed request_id=%s error_class=%s",
        request_id,
        type(error).__name__,
    )

    if isinstance(error, GarminAuthenticationError):
        return PublicError(
            "AUTH_REQUIRED",
            "authentication_error",
            "Garmin authentication is required. Run 'garmin-connect-mcp auth'.",
            request_id,
        )
    if isinstance(error, GarminRateLimitError):
        return PublicError(
            "RATE_LIMITED",
            "rate_limit_error",
            "Garmin rate limit exceeded. Wait a few minutes and try again.",
            request_id,
        )
    if isinstance(error, GarminNotFoundError):
        return PublicError(
            "NOT_FOUND",
            "not_found",
            "The requested Garmin data was not found.",
            request_id,
        )
    if isinstance(error, GarminMethodNotAllowedError):
        return PublicError(
            "CAPABILITY_UNAVAILABLE",
            "capability_unavailable",
            "This Garmin capability is disabled by server policy. Enable it explicitly in "
            "the server configuration before retrying.",
            request_id,
        )
    if isinstance(error, GarminMethodUnavailableError):
        return PublicError(
            "CAPABILITY_UNAVAILABLE",
            "capability_unavailable",
            "This Garmin capability is unavailable in the installed dependency version.",
            request_id,
        )
    if isinstance(error, GarminMutationOutcomeUnknownError):
        return PublicError(
            "GARMIN_UPSTREAM_UNAVAILABLE",
            "api_error",
            "Garmin may have completed the mutation without confirming it. Reconcile the "
            "result before retrying, and do not use a new idempotency_key.",
            request_id,
        )
    if isinstance(error, GarminMutationInProgressError):
        return PublicError(
            "GARMIN_UPSTREAM_UNAVAILABLE",
            "api_error",
            "Another Garmin mutation is still running. Wait and retry with the same "
            "idempotency_key.",
            request_id,
        )
    if isinstance(error, GarminAPIError):
        return PublicError(
            getattr(error, "public_code", "GARMIN_UPSTREAM_UNAVAILABLE"),
            "api_error",
            "Garmin Connect could not complete the request. Try again later.",
            request_id,
        )
    return PublicError(
        "INTERNAL_ERROR",
        "internal_error",
        "An internal error prevented the Garmin request from completing.",
        request_id,
    )


def default_error_code(error_type: str) -> str:
    """Map existing bounded validation categories to stable public codes."""
    return {
        "validation_error": "VALIDATION_ERROR",
        "invalid_parameters": "VALIDATION_ERROR",
        "insufficient_data": "VALIDATION_ERROR",
        "not_found": "NOT_FOUND",
        "capability_unavailable": "CAPABILITY_UNAVAILABLE",
        "authentication_error": "AUTH_REQUIRED",
        "rate_limit_error": "RATE_LIMITED",
        "api_error": "GARMIN_UPSTREAM_UNAVAILABLE",
        "internal_error": "INTERNAL_ERROR",
    }.get(error_type, "INTERNAL_ERROR")
