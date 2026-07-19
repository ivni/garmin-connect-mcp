"""Garmin Connect API client wrapper with error handling."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .token_store import TokenStoreLockTimeout

logger = logging.getLogger(__name__)


class GarminAPIError(Exception):
    """Custom exception for Garmin API errors."""

    public_code = "GARMIN_UPSTREAM_UNAVAILABLE"

    def __init__(self, message: str, original_error: Exception | None = None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


class GarminRateLimitError(GarminAPIError):
    """Exception raised when rate limit is exceeded (HTTP 429)."""

    public_code = "RATE_LIMITED"

    def __init__(self, original_error: Exception | None = None):
        super().__init__(
            "Rate limit exceeded. Please wait a few minutes before trying again.",
            original_error=original_error,
        )


class GarminNotFoundError(GarminAPIError):
    """Exception raised when resource is not found (HTTP 404)."""

    public_code = "NOT_FOUND"

    def __init__(self, resource: str = "Resource", original_error: Exception | None = None):
        super().__init__(
            f"{resource} not found. Please check the ID or date and try again.",
            original_error=original_error,
        )


class GarminAuthenticationError(GarminAPIError):
    """Exception raised when authentication fails (HTTP 401/403)."""

    public_code = "AUTH_REQUIRED"

    def __init__(
        self,
        message: str = "Authentication failed. Please run 'garmin-connect-mcp auth'.",
        original_error: Exception | None = None,
    ):
        super().__init__(
            message,
            original_error=original_error,
        )


class GarminMethodNotAllowedError(GarminAPIError):
    """Raised when a restricted facade is asked to call an unapproved method."""

    public_code = "CAPABILITY_UNAVAILABLE"


class GarminMethodUnavailableError(GarminAPIError):
    """Raised when the underlying Garmin client lacks a method before dispatch."""

    public_code = "CAPABILITY_UNAVAILABLE"


class GarminMutationOutcomeUnknownError(GarminAPIError):
    """Raised when Garmin may have committed a mutation without confirming it."""

    public_code = "GARMIN_UPSTREAM_UNAVAILABLE"


class GarminMutationInProgressError(GarminAPIError):
    """Raised when another process still owns the serialized mutation transaction."""

    public_code = "GARMIN_UPSTREAM_UNAVAILABLE"


@dataclass(frozen=True)
class MutationOperation:
    """Least-privilege contract for one Garmin mutation method."""

    capability: str
    method_name: str
    reconciliation: str


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class _MemoryMutationJournal:
    """In-memory journal used by isolated tests and non-session callers."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, str]] = {}

    @contextlib.contextmanager
    def mutation_transaction(self):
        """Keep one in-memory mutation transaction live through its invocation."""
        with self._lock:
            yield

    def update_mutation_records(
        self,
        update: Callable[[dict[str, dict[str, str]]], tuple[Any, bool]],
    ) -> Any:
        with self._lock:
            result, _changed = update(self._records)
            return result


class MutationRegistry:
    """Durable de-duplication and ambiguous-outcome quarantine."""

    def __init__(self, max_records: int = 1024, journal: Any | None = None):
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._max_records = max_records
        self._lock = threading.RLock()
        self._journal = journal or _MemoryMutationJournal()
        self._results: dict[tuple[str, str], Any] = {}

    def execute(
        self,
        operation: MutationOperation,
        idempotency_key: str,
        method_args: tuple[Any, ...],
        method_kwargs: dict[str, Any],
        invoke: Callable[[], Any],
    ) -> Any:
        """Execute once per capability/key and quarantine uncertain outcomes."""
        _validate_idempotency_key(idempotency_key)
        record_key = hashlib.sha256(
            f"{operation.capability}\0{idempotency_key}".encode()
        ).hexdigest()
        fingerprint = _mutation_fingerprint(
            operation.method_name,
            method_args,
            method_kwargs,
        )
        audit_key = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:12]

        def reserve(records: dict[str, dict[str, str]]) -> tuple[str, bool]:
            existing = records.get(record_key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise GarminAPIError(
                        "The idempotency key was already used with different mutation inputs. "
                        "Use the original inputs or a new key."
                    )
                if existing["state"] == "in_progress":
                    existing["state"] = "unknown"
                    return "unknown", True
                return existing["state"], False
            if len(records) >= self._max_records:
                raise GarminAPIError(
                    "The durable mutation safety ledger is full. New writes are refused so "
                    "confirmed idempotency keys cannot be evicted and replayed."
                )
            records[record_key] = {
                "capability": operation.capability,
                "method_name": operation.method_name,
                "fingerprint": fingerprint,
                "state": "in_progress",
            }
            return "reserved", True

        # The journal transaction remains locked across the remote call. A concurrent
        # caller therefore cannot observe an active reservation. If ``reserve`` sees
        # ``in_progress``, the former lock owner exited without finalizing (for example,
        # process termination), so quarantining it as unknown is explicit crash recovery.
        with contextlib.ExitStack() as transaction_stack:
            try:
                transaction_stack.enter_context(self._journal.mutation_transaction())
            except TokenStoreLockTimeout as exc:
                raise GarminMutationInProgressError(
                    "Another Garmin mutation is still running. Wait, then retry this request "
                    "with the same idempotency_key; do not use a new key while it is running.",
                    original_error=exc,
                ) from exc

            state = self._journal.update_mutation_records(reserve)
            if state == "succeeded":
                logger.info(
                    "Garmin mutation deduplicated capability=%s method=%s key=%s",
                    operation.capability,
                    operation.method_name,
                    audit_key,
                )
                with self._lock:
                    if (record_key, fingerprint) in self._results:
                        return self._results[(record_key, fingerprint)]
                return {
                    "deduplicated": True,
                    "message": (
                        "A confirmed idempotency record exists; Garmin was not called again."
                    ),
                }
            if state == "unknown":
                raise _unknown_outcome_error(operation, "unknown")

            logger.info(
                "Garmin mutation started capability=%s method=%s key=%s",
                operation.capability,
                operation.method_name,
                audit_key,
            )
            try:
                result = invoke()
            except Exception as exc:
                if _is_ambiguous_mutation_error(exc):
                    with contextlib.suppress(Exception):
                        self._set_state(record_key, fingerprint, "unknown")
                    logger.warning(
                        "Garmin mutation outcome unknown capability=%s method=%s key=%s",
                        operation.capability,
                        operation.method_name,
                        audit_key,
                    )
                    raise _unknown_outcome_error(operation, "unknown", exc) from exc
                try:
                    self._remove_reservation(record_key, fingerprint)
                except Exception as cleanup_error:
                    raise GarminAPIError(
                        "The mutation failed before a confirmed outcome, but its safety "
                        "reservation could not be cleared. Do not retry until the ledger "
                        "is repaired.",
                        original_error=cleanup_error,
                    ) from cleanup_error
                raise

            try:
                self._set_state(record_key, fingerprint, "succeeded")
            except Exception as exc:
                raise _unknown_outcome_error(operation, "unknown", exc) from exc
            with self._lock:
                self._results[(record_key, fingerprint)] = result
            logger.info(
                "Garmin mutation succeeded capability=%s method=%s key=%s",
                operation.capability,
                operation.method_name,
                audit_key,
            )
            return result

    def _set_state(self, record_key: str, fingerprint: str, state: str) -> None:
        def update(records: dict[str, dict[str, str]]) -> tuple[None, bool]:
            record = records.get(record_key)
            if record is None or record["fingerprint"] != fingerprint:
                raise GarminAPIError("The durable mutation reservation was lost.")
            record["state"] = state
            return None, True

        self._journal.update_mutation_records(update)

    def _remove_reservation(self, record_key: str, fingerprint: str) -> None:
        def update(records: dict[str, dict[str, str]]) -> tuple[None, bool]:
            record = records.get(record_key)
            if record is None:
                return None, False
            if record["fingerprint"] != fingerprint:
                raise GarminAPIError("The durable mutation reservation changed unexpectedly.")
            del records[record_key]
            return None, True

        self._journal.update_mutation_records(update)


class GarminReadClient:
    """Facade that exposes only explicitly classified read methods."""

    def __init__(self, client: GarminClientWrapper, allowed_methods: frozenset[str]):
        self._client = client
        self._allowed_methods = allowed_methods

    def safe_call(self, method_name: str, *args, **kwargs) -> Any:
        if method_name not in self._allowed_methods:
            raise GarminMethodNotAllowedError(
                f"Garmin method '{method_name}' is not available through the read-only client."
            )
        return self._client.safe_call(method_name, *args, **kwargs)


class GarminMutationClient:
    """Facade restricted to one enabled mutation capability and method."""

    def __init__(
        self,
        client: GarminClientWrapper,
        operation: MutationOperation,
        registry: MutationRegistry,
    ):
        self._client = client
        self._operation = operation
        self._registry = registry

    def mutate(
        self,
        method_name: str,
        *args,
        idempotency_key: str,
        **kwargs,
    ) -> Any:
        if method_name != self._operation.method_name:
            raise GarminMethodNotAllowedError(
                f"Garmin method '{method_name}' is not authorized by capability "
                f"'{self._operation.capability}'."
            )
        return self._registry.execute(
            self._operation,
            idempotency_key,
            args,
            kwargs,
            lambda: self._client.safe_call(method_name, *args, **kwargs),
        )


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise GarminAPIError(
            "idempotency_key must be 8-128 characters and contain only letters, "
            "numbers, '.', '_', ':', or '-'."
        )


def _mutation_fingerprint(
    method_name: str,
    method_args: tuple[Any, ...],
    method_kwargs: dict[str, Any],
) -> str:
    serialized = json.dumps(
        [method_name, method_args, method_kwargs],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=repr,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_ambiguous_mutation_error(error: Exception) -> bool:
    if isinstance(error, GarminMutationOutcomeUnknownError):
        return True
    if isinstance(
        error,
        (
            GarminAuthenticationError,
            GarminMethodNotAllowedError,
            GarminMethodUnavailableError,
            GarminNotFoundError,
            GarminRateLimitError,
        ),
    ):
        return False

    original = error.original_error if isinstance(error, GarminAPIError) else error
    status_code = _http_status_code(original)
    if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 408:
        return False

    # Once invocation begins, generic type/value/parsing/transport failures cannot prove
    # that Garmin rejected the write. Quarantine unless the cases above establish a
    # pre-dispatch failure or a definite client-side HTTP rejection.
    return True


def _http_status_code(error: Exception) -> int | None:
    """Extract a trustworthy response status without inspecting server detail text."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    if isinstance(error, GarminConnectConnectionError):
        match = re.match(r"^API Error (\d{3})(?:\b|:)", str(error))
        if match:
            return int(match.group(1))
    return None


def _unknown_outcome_error(
    operation: MutationOperation,
    state: str,
    original_error: Exception | None = None,
) -> GarminMutationOutcomeUnknownError:
    reason = "is still in progress" if state == "in_progress" else "was not confirmed"
    return GarminMutationOutcomeUnknownError(
        f"The Garmin mutation {reason} and must not be retried automatically. "
        f"Reconcile it first: {operation.reconciliation}",
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
                    try:
                        method = getattr(self.client, method_name)
                    except AttributeError as exc:
                        raise GarminMethodUnavailableError(
                            f"Method '{method_name}' not found on Garmin client",
                            original_error=exc,
                        ) from exc
                    result = method(*args, **kwargs)
                except Exception:
                    self._run_after_call()
                    raise
                self._run_after_call()
                return result
        except GarminConnectAuthenticationError as e:
            if self._on_authentication_error is not None:
                self._on_authentication_error()
            raise GarminAuthenticationError(original_error=e) from e
        except GarminConnectTooManyRequestsError as e:
            raise GarminRateLimitError(original_error=e) from e
        except GarminConnectConnectionError as e:
            status_code = _http_status_code(e)
            if status_code == 429:
                raise GarminRateLimitError(original_error=e) from e
            elif status_code == 404:
                raise GarminNotFoundError(original_error=e) from e
            elif status_code in {401, 403}:
                if self._on_authentication_error is not None:
                    self._on_authentication_error()
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(
                    "Garmin Connect could not complete the request. Try again later.",
                    original_error=e,
                ) from e
        except GarminAPIError:
            raise
        except Exception as e:
            raise GarminAPIError("Garmin returned an unexpected response.", original_error=e) from e

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
                "the cached session was invalidated error_class=%s",
                type(exc).__name__,
            )

    def _assert_active(self) -> None:
        if self._revoked:
            raise GarminAuthenticationError(
                "This Garmin session generation was revoked. Retry the request."
            )
