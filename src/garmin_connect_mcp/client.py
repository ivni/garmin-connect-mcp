"""Garmin Connect API client wrapper with error handling."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

logger = logging.getLogger(__name__)


class GarminAPIError(Exception):
    """Custom exception for Garmin API errors."""

    def __init__(self, message: str, original_error: Exception | None = None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


class GarminRateLimitError(GarminAPIError):
    """Exception raised when rate limit is exceeded (HTTP 429)."""

    def __init__(self, original_error: Exception | None = None):
        super().__init__(
            "Rate limit exceeded. Please wait a few minutes before trying again.",
            original_error=original_error,
        )


class GarminNotFoundError(GarminAPIError):
    """Exception raised when resource is not found (HTTP 404)."""

    def __init__(self, resource: str = "Resource", original_error: Exception | None = None):
        super().__init__(
            f"{resource} not found. Please check the ID or date and try again.",
            original_error=original_error,
        )


class GarminAuthenticationError(GarminAPIError):
    """Exception raised when authentication fails (HTTP 401/403)."""

    def __init__(
        self,
        message: str = "Authentication failed. Please run 'garmin-connect-mcp auth'.",
        original_error: Exception | None = None,
    ):
        super().__init__(
            message,
            original_error=original_error,
        )


class GarminClientWrapper:
    """Wrapper around Garmin client for consistent error handling."""

    def __init__(
        self,
        client: Garmin,
        lock: threading.RLock | None = None,
        before_call: Callable[[], None] | None = None,
        after_call: Callable[[], None] | None = None,
        on_authentication_error: Callable[[], None] | None = None,
    ):
        self.client = client
        self._lock = lock
        self._before_call = before_call
        self._after_call = after_call
        self._on_authentication_error = on_authentication_error
        self._revoked = False

    def revoke(self) -> None:
        """Permanently prevent this generation wrapper from making another request."""
        self._revoked = True

    def safe_call(self, method_name: str, *args, **kwargs) -> Any:
        """
        Safely call a Garmin client method with error handling.

        This method uses `Any` as the return type because it dynamically proxies calls
        to the external garminconnect library, which doesn't have type stubs. The actual
        return type depends on which Garmin API method is called.

        Args:
            method_name: Name of the Garmin client method to call
            *args: Positional arguments for the method
            **kwargs: Keyword arguments for the method

        Returns:
            Method result or raises GarminAPIError

        Raises:
            GarminAuthenticationError: Authentication failed (401/403)
            GarminNotFoundError: Resource not found (404)
            GarminRateLimitError: Rate limit exceeded (429)
            GarminAPIError: Other API errors
        """
        lock_context = self._lock or contextlib.nullcontext()
        try:
            with lock_context:
                self._assert_active()
                if self._before_call is not None:
                    self._before_call()
                self._assert_active()
                try:
                    method = getattr(self.client, method_name)
                    result = method(*args, **kwargs)
                except Exception:
                    self._run_after_call()
                    raise
                self._run_after_call()
                return result
        except AttributeError as e:
            raise GarminAPIError(
                f"Method '{method_name}' not found on Garmin client", original_error=e
            ) from e
        except GarminConnectAuthenticationError as e:
            if self._on_authentication_error is not None:
                self._on_authentication_error()
            raise GarminAuthenticationError(original_error=e) from e
        except GarminConnectTooManyRequestsError as e:
            raise GarminRateLimitError(original_error=e) from e
        except GarminConnectConnectionError as e:
            # Parse HTTP status code from error
            error_str = str(e)
            if "429" in error_str or "Too Many Requests" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str or "Not Found" in error_str:
                raise GarminNotFoundError(original_error=e) from e
            elif "401" in error_str or "403" in error_str or "Unauthorized" in error_str:
                if self._on_authentication_error is not None:
                    self._on_authentication_error()
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except GarminAPIError:
            raise
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

    def _run_after_call(self) -> None:
        """Persist refresh state without hiding an already completed API call."""
        if self._after_call is None:
            return
        try:
            self._after_call()
        except Exception as exc:
            self.revoke()
            # A remote mutation may already be committed. Surfacing a later
            # housekeeping error would invite an unsafe retry or duplicate.
            logger.warning(
                "Garmin API call completed, but refreshed-token persistence failed; "
                "the cached session was invalidated: %s",
                exc,
            )

    def _assert_active(self) -> None:
        if self._revoked:
            raise GarminAuthenticationError(
                "This Garmin session generation was revoked. Retry the request."
            )
