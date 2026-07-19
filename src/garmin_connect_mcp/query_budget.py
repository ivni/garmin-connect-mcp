"""Deterministic per-request resource budgets for MCP surfaces."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import CancelledError as ConcurrentCancelledError
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from types import MappingProxyType
from typing import Any

from .client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminInvalidIdempotencyKeyError,
    GarminMethodNotAllowedError,
    GarminMethodUnavailableError,
    GarminMutationInProgressError,
    GarminMutationOutcomeUnknownError,
    GarminNotFoundError,
    GarminRateLimitError,
    validate_idempotency_key,
)
from .time_utils import parse_date_string

logger = logging.getLogger(__name__)

POLICY_VERSION = "1"
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
MIN_MAX_RESPONSE_BYTES = 4 * 1024
MAX_GARMIN_READ_WORKERS = 8
MAX_GARMIN_MUTATION_WORKERS = 2
MAX_FINALIZATION_WORKERS = 4

_GARMIN_READ_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_GARMIN_READ_WORKERS,
    thread_name_prefix="garmin-mcp-read",
)
_GARMIN_MUTATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_GARMIN_MUTATION_WORKERS,
    thread_name_prefix="garmin-mcp-mutation",
)
_FINALIZATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=MAX_FINALIZATION_WORKERS,
    thread_name_prefix="garmin-mcp-finalize",
)
_GARMIN_READ_ADMISSION = threading.BoundedSemaphore(MAX_GARMIN_READ_WORKERS)
_GARMIN_MUTATION_ADMISSION = threading.BoundedSemaphore(MAX_GARMIN_MUTATION_WORKERS)
_FINALIZATION_ADMISSION = threading.BoundedSemaphore(MAX_FINALIZATION_WORKERS)
_UNSET_MUTATION_RESULT = object()


async def _acquire_executor_slot(
    admission: threading.BoundedSemaphore,
    budget: RequestBudget,
) -> None:
    """Wait cooperatively for bounded executor admission within the request deadline."""
    while not admission.acquire(blocking=False):
        budget.ensure_active()
        await asyncio.sleep(min(0.01, budget.remaining_seconds()))
    try:
        budget.ensure_active()
    except BaseException:
        admission.release()
        raise


def _release_executor_slot(
    future: ConcurrentFuture[Any],
    *,
    admission: threading.BoundedSemaphore,
) -> None:
    """Release bounded admission and retrieve any abandoned worker exception."""
    try:
        future.exception()
    except ConcurrentCancelledError:
        pass
    finally:
        admission.release()


def _drain_async_future(future: asyncio.Future[Any]) -> None:
    """Prevent an abandoned asyncio wrapper from reporting a raw worker exception."""
    if future.cancelled():
        return
    try:
        future.exception()
    except asyncio.CancelledError:
        pass


def _submit_bounded(
    executor: ThreadPoolExecutor,
    admission: threading.BoundedSemaphore,
    invoke: Callable[[], Any],
) -> tuple[ConcurrentFuture[Any], asyncio.Future[Any]]:
    """Submit only after admission; release the slot when the worker truly finishes."""
    try:
        concurrent_future = executor.submit(invoke)
    except BaseException:
        admission.release()
        raise
    concurrent_future.add_done_callback(partial(_release_executor_slot, admission=admission))
    async_future = asyncio.wrap_future(concurrent_future)
    async_future.add_done_callback(_drain_async_future)
    return concurrent_future, async_future


async def run_bounded_finalization(
    invoke: Callable[[], Any],
    budget: RequestBudget,
) -> Any:
    """Run final wire serialization in a bounded pool within the request deadline."""
    await _acquire_executor_slot(_FINALIZATION_ADMISSION, budget)
    _concurrent_future, async_future = _submit_bounded(
        _FINALIZATION_EXECUTOR,
        _FINALIZATION_ADMISSION,
        invoke,
    )
    try:
        async with asyncio.timeout(budget.remaining_seconds()):
            return await asyncio.shield(async_future)
    except TimeoutError as exc:
        budget.expire()
        raise RequestDeadlineExceededError from exc
    except asyncio.CancelledError:
        budget.cancel()
        raise


class QueryBudgetError(Exception):
    """Base class for stable, public resource-budget failures."""

    public_code = "RESOURCE_BUDGET_EXCEEDED"
    error_type = "resource_budget_error"
    public_message = "The request exceeds the server resource budget."


class InvalidDateRangeError(QueryBudgetError):
    public_code = "INVALID_DATE_RANGE"
    error_type = "invalid_parameters"
    public_message = "Provide a valid date range with start_date on or before end_date."


class RangeTooLargeError(QueryBudgetError):
    public_code = "RANGE_TOO_LARGE"
    error_type = "invalid_parameters"

    def __init__(self, maximum_days: int):
        self.maximum_days = maximum_days
        self.public_message = (
            f"The requested date range exceeds the maximum of {maximum_days} days."
        )
        super().__init__(self.public_message)


class ApiCallBudgetExceededError(QueryBudgetError):
    public_code = "API_CALL_BUDGET_EXCEEDED"
    public_message = "The request would exceed the Garmin API-call budget."


class ResponseBudgetExceededError(QueryBudgetError):
    public_code = "RESPONSE_BUDGET_EXCEEDED"
    public_message = "The projected response exceeds the maximum serialized size."


class ResultItemBudgetExceededError(QueryBudgetError):
    public_code = "ITEM_BUDGET_EXCEEDED"
    public_message = "The upstream result exceeds the maximum number of response items."


class RequestDeadlineExceededError(QueryBudgetError):
    public_code = "REQUEST_DEADLINE_EXCEEDED"
    public_message = "The request exceeded its execution deadline."


class RequestCancelledError(QueryBudgetError):
    public_code = "REQUEST_CANCELLED"
    error_type = "cancelled"
    public_message = "The request was cancelled before completion."


class InvalidContinuationCursorError(QueryBudgetError):
    public_code = "INVALID_CONTINUATION_CURSOR"
    error_type = "invalid_parameters"
    public_message = "The continuation cursor is invalid for this query."


class InvalidPageSizeError(QueryBudgetError):
    public_code = "INVALID_PAGE_SIZE"
    error_type = "invalid_parameters"

    def __init__(self, maximum_items: int):
        self.maximum_items = maximum_items
        self.public_message = f"Page size must be between 1 and {maximum_items}."
        super().__init__(self.public_message)


class UpstreamRateLimitTerminatedError(QueryBudgetError):
    """Preserve a rate-limit response while making it terminal to fan-out."""

    public_code = "RATE_LIMITED"
    error_type = "rate_limit_error"
    public_message = "Garmin rate limit exceeded. Wait a few minutes and try again."


class UpstreamAuthenticationTerminatedError(QueryBudgetError):
    """Preserve an auth response while making it terminal to fan-out."""

    public_code = "AUTH_REQUIRED"
    error_type = "authentication_error"
    public_message = "Garmin authentication is required. Run 'garmin-connect-mcp auth'."


class UpstreamFailureTerminatedError(QueryBudgetError):
    """Make transport and unexpected upstream failures terminal to fan-out."""

    public_code = "GARMIN_UPSTREAM_UNAVAILABLE"
    error_type = "api_error"
    public_message = "Garmin Connect could not complete the request. Try again later."


@dataclass(frozen=True)
class QueryBudgetPolicy:
    """Public resource envelope for one MCP tool or resource."""

    max_range_days: int | None
    default_page_items: int
    max_page_items: int
    max_api_calls: int
    max_aggregate_items: int | None = None
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timeout_seconds: float = 30.0
    partial_results_allowed: bool = False

    def __post_init__(self) -> None:
        numeric_values = (
            self.default_page_items,
            self.max_page_items,
            self.max_api_calls,
            self.max_response_bytes,
        )
        if any(value <= 0 for value in numeric_values):
            raise ValueError("Query budget values must be positive")
        if self.max_response_bytes < MIN_MAX_RESPONSE_BYTES:
            raise ValueError(
                f"Maximum response size cannot be below {MIN_MAX_RESPONSE_BYTES} bytes"
            )
        if self.default_page_items > self.max_page_items:
            raise ValueError("Default page size cannot exceed maximum page size")
        if self.max_aggregate_items is not None and self.max_aggregate_items < self.max_page_items:
            raise ValueError("Aggregate item ceiling cannot be below the page ceiling")
        if self.max_range_days is not None and self.max_range_days <= 0:
            raise ValueError("Maximum range must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("Request timeout must be positive")


def _policy(
    *,
    max_range_days: int | None = None,
    default_page_items: int | None = None,
    max_page_items: int = 50,
    max_api_calls: int = 25,
    max_aggregate_items: int | None = None,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    timeout_seconds: float = 30.0,
    partial_results_allowed: bool = False,
) -> QueryBudgetPolicy:
    resolved_default_page_items = (
        min(20, max_page_items) if default_page_items is None else default_page_items
    )
    return QueryBudgetPolicy(
        max_range_days=max_range_days,
        default_page_items=resolved_default_page_items,
        max_page_items=max_page_items,
        max_api_calls=max_api_calls,
        max_aggregate_items=max_aggregate_items,
        max_response_bytes=max_response_bytes,
        timeout_seconds=timeout_seconds,
        partial_results_allowed=partial_results_allowed,
    )


_DEFAULT_READ_POLICY = _policy()
_WRITE_POLICY = _policy(
    default_page_items=1,
    max_page_items=1,
    max_api_calls=1,
    max_response_bytes=64 * 1024,
)

BUDGET_POLICIES: Mapping[str, QueryBudgetPolicy] = MappingProxyType(
    {
        "query_activities": _policy(
            max_range_days=366,
            default_page_items=20,
            max_page_items=50,
            max_api_calls=25,
            partial_results_allowed=True,
        ),
        "get_activity_details": _policy(max_api_calls=7),
        "get_activity_social": _policy(max_api_calls=1),
        "compare_activities": _policy(max_page_items=5, max_api_calls=5),
        "find_similar_activities": _policy(
            default_page_items=10,
            max_page_items=20,
            max_api_calls=2,
        ),
        "query_health_summary": _policy(
            max_range_days=366,
            default_page_items=7,
            max_page_items=30,
            max_api_calls=100,
            partial_results_allowed=True,
        ),
        "query_sleep_data": _policy(
            max_range_days=31,
            default_page_items=7,
            max_page_items=31,
            max_api_calls=40,
            partial_results_allowed=True,
        ),
        "query_heart_rate_data": _policy(
            max_range_days=31,
            default_page_items=7,
            max_page_items=31,
            max_api_calls=70,
            partial_results_allowed=True,
        ),
        "query_activity_metrics": _policy(
            max_range_days=31,
            default_page_items=7,
            max_page_items=31,
            max_api_calls=100,
            partial_results_allowed=True,
        ),
        "query_devices": _policy(max_range_days=31, max_page_items=31, max_api_calls=8),
        "query_gear": _DEFAULT_READ_POLICY,
        "get_user_profile": _policy(max_api_calls=8),
        "query_goals_and_records": _policy(max_api_calls=2),
        "query_challenges": _policy(
            default_page_items=50,
            max_page_items=50,
            max_aggregate_items=250,
            max_api_calls=10,
        ),
        "analyze_training_period": _policy(
            max_range_days=366,
            default_page_items=54,
            max_page_items=54,
            max_aggregate_items=108,
            max_api_calls=25,
        ),
        "get_performance_metrics": _policy(
            max_range_days=366,
            max_page_items=2000,
            max_api_calls=10,
        ),
        "get_training_effect": _policy(
            max_range_days=366,
            max_page_items=366,
            max_api_calls=5,
        ),
        "query_weight_data": _policy(
            max_range_days=366,
            max_page_items=366,
            max_api_calls=5,
        ),
        "add_weight_entry": _WRITE_POLICY,
        "delete_weight_entries": _policy(
            default_page_items=10,
            max_page_items=10,
            max_api_calls=11,
            max_response_bytes=64 * 1024,
        ),
        "query_workouts": _policy(max_api_calls=2, partial_results_allowed=True),
        "upload_workout": _WRITE_POLICY,
        "log_body_composition": _WRITE_POLICY,
        "log_blood_pressure": _WRITE_POLICY,
        "log_hydration": _WRITE_POLICY,
        "query_womens_health": _policy(
            max_range_days=366,
            max_page_items=366,
            max_api_calls=5,
        ),
        "garmin://athlete/profile": _policy(max_api_calls=4),
        "garmin://training/readiness": _policy(max_api_calls=2),
        "garmin://health/today": _policy(max_api_calls=2),
    }
)

RANGE_SURFACE_ARGUMENTS: Mapping[str, tuple[str, str] | tuple[str]] = MappingProxyType(
    {
        "query_activities": ("start_date", "end_date"),
        "query_health_summary": ("start_date", "end_date"),
        "query_sleep_data": ("start_date", "end_date"),
        "query_heart_rate_data": ("start_date", "end_date"),
        "query_activity_metrics": ("start_date", "end_date"),
        "query_devices": ("solar_start_date", "solar_end_date"),
        "analyze_training_period": ("period",),
        "get_performance_metrics": ("start_date", "end_date"),
        "get_training_effect": ("start_date", "end_date"),
        "query_weight_data": ("start_date", "end_date"),
        "query_womens_health": ("start_date", "end_date"),
    }
)


def policy_for_surface(surface: str) -> QueryBudgetPolicy:
    """Return the explicit policy for a registered public surface."""
    try:
        return BUDGET_POLICIES[surface]
    except KeyError as exc:
        raise RuntimeError(f"No query budget policy registered for surface {surface!r}") from exc


@dataclass(frozen=True)
class BoundedDateRange:
    """Validated inclusive date range that is cheap to page."""

    start: date
    end: date
    day_count: int

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()

    def page(self, position: int, page_size: int) -> tuple[tuple[str, ...], int | None]:
        """Return one arithmetic date page and the next zero-based position."""
        if position < 0 or position > self.day_count:
            raise InvalidDateRangeError
        if page_size <= 0:
            raise InvalidDateRangeError
        count = min(page_size, self.day_count - position)
        values = tuple(
            (self.start + timedelta(days=position + offset)).isoformat() for offset in range(count)
        )
        next_position = position + count
        return values, next_position if next_position < self.day_count else None

    def iter_dates(self):
        """Yield ISO dates without materializing the full range."""
        for offset in range(self.day_count):
            yield (self.start + timedelta(days=offset)).isoformat()


def _as_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise TypeError("Date values must be strings or date objects")
    return parse_date_string(value).date()


def validate_date_range(
    start_value: str | date | datetime | None,
    end_value: str | date | datetime | None,
    *,
    policy: QueryBudgetPolicy,
) -> BoundedDateRange:
    """Validate a paired inclusive range without materializing its dates.

    Future dates remain allowed for compatibility. They still count toward every
    configured range and request budget.
    """
    if start_value is None or end_value is None:
        raise InvalidDateRangeError
    try:
        start = _as_date(start_value)
        end = _as_date(end_value)
    except (TypeError, ValueError) as exc:
        raise InvalidDateRangeError from exc
    if start > end:
        raise InvalidDateRangeError
    day_count = (end - start).days + 1
    budget = current_request_budget()
    if budget is not None:
        budget.note_requested_range_days(day_count)
    if policy.max_range_days is not None and day_count > policy.max_range_days:
        raise RangeTooLargeError(policy.max_range_days)
    return BoundedDateRange(start, end, day_count)


def validate_page_size(value: str | int | None, policy: QueryBudgetPolicy) -> int:
    """Parse and validate one endpoint page size."""
    if value is None:
        return policy.default_page_items
    if isinstance(value, bool):
        raise InvalidPageSizeError(policy.max_page_items)
    try:
        page_size = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidPageSizeError(policy.max_page_items) from exc
    if not 1 <= page_size <= policy.max_page_items:
        raise InvalidPageSizeError(policy.max_page_items)
    return page_size


class RequestBudget:
    """Thread-safe runtime budget belonging to one MCP request."""

    def __init__(self, surface: str, policy: QueryBudgetPolicy):
        self.surface = surface
        self.policy = policy
        self._started = time.monotonic()
        self._deadline = self._started + policy.timeout_seconds
        self._calls_used = 0
        self._items_used = 0
        self._response_bytes = 0
        self._terminal_reason: str | None = None
        self._outcome_hint: str | None = None
        self._mutation_dispatched = False
        self._mutation_outcome = "not_dispatched"
        self._requested_range_days = 0
        self._truncation_reason: str | None = None
        self._continuation_emitted = False
        self._lock = threading.Lock()

    @property
    def calls_used(self) -> int:
        with self._lock:
            return self._calls_used

    @property
    def items_used(self) -> int:
        with self._lock:
            return self._items_used

    @property
    def response_bytes(self) -> int:
        with self._lock:
            return self._response_bytes

    @property
    def terminal_reason(self) -> str | None:
        with self._lock:
            return self._terminal_reason

    @property
    def outcome_hint(self) -> str | None:
        with self._lock:
            return self._outcome_hint

    @property
    def requested_range_days(self) -> int:
        with self._lock:
            return self._requested_range_days

    @property
    def truncation_reason(self) -> str | None:
        with self._lock:
            return self._truncation_reason

    @property
    def continuation_emitted(self) -> bool:
        with self._lock:
            return self._continuation_emitted

    @property
    def mutation_confirmed(self) -> bool:
        with self._lock:
            return self._mutation_outcome == "confirmed"

    @property
    def mutation_uncertain(self) -> bool:
        with self._lock:
            return self._mutation_outcome == "uncertain"

    @property
    def mutation_outcome(self) -> str:
        with self._lock:
            return self._mutation_outcome

    @property
    def mutation_dispatched(self) -> bool:
        with self._lock:
            return self._mutation_dispatched

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self._started)

    def remaining_seconds(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self.expire()
            raise RequestDeadlineExceededError
        return remaining

    def ensure_active(self) -> None:
        with self._lock:
            terminal = self._terminal_reason
        self._raise_for_terminal(terminal)
        if time.monotonic() >= self._deadline:
            self.expire()
            raise RequestDeadlineExceededError

    @staticmethod
    def _raise_for_terminal(terminal: str | None) -> None:
        if terminal == "cancelled":
            raise RequestCancelledError
        if terminal == "deadline":
            raise RequestDeadlineExceededError
        if terminal == "api_calls":
            raise ApiCallBudgetExceededError
        if terminal == "response_bytes":
            raise ResponseBudgetExceededError
        if terminal == "items":
            raise ResultItemBudgetExceededError
        if terminal == "upstream_rate_limit":
            raise UpstreamRateLimitTerminatedError
        if terminal == "upstream_authentication":
            raise UpstreamAuthenticationTerminatedError
        if terminal == "upstream_failure":
            raise UpstreamFailureTerminatedError
        if terminal is not None:
            raise QueryBudgetError

    def require_calls(self, count: int) -> None:
        """Fail before work starts when the minimum call cost cannot fit."""
        if count < 0:
            raise ValueError("Required call count cannot be negative")
        with self._lock:
            self._raise_for_terminal(self._terminal_reason)
            if time.monotonic() >= self._deadline:
                self._terminal_reason = "deadline"
                raise RequestDeadlineExceededError
            if self._calls_used + count > self.policy.max_api_calls:
                self._terminal_reason = "api_calls"
                raise ApiCallBudgetExceededError

    def reserve_call(self) -> int:
        with self._lock:
            self._raise_for_terminal(self._terminal_reason)
            if time.monotonic() >= self._deadline:
                self._terminal_reason = "deadline"
                raise RequestDeadlineExceededError
            if self._calls_used + 1 > self.policy.max_api_calls:
                self._terminal_reason = "api_calls"
                raise ApiCallBudgetExceededError
            self._calls_used += 1
            return self._calls_used

    def reserve_items(self, count: int) -> None:
        if count < 0:
            raise ValueError("Item count cannot be negative")
        with self._lock:
            self._raise_for_terminal(self._terminal_reason)
            if time.monotonic() >= self._deadline:
                self._terminal_reason = "deadline"
                raise RequestDeadlineExceededError
            item_ceiling = self.policy.max_aggregate_items or self.policy.max_page_items
            if self._items_used + count > item_ceiling:
                self._terminal_reason = "items"
                raise ResultItemBudgetExceededError
            self._items_used += count

    def record_response_size(self, byte_count: int) -> None:
        if byte_count < 0:
            raise ValueError("Response size cannot be negative")
        with self._lock:
            self._response_bytes = byte_count
            if byte_count > self.policy.max_response_bytes:
                self._terminal_reason = "response_bytes"
                raise ResponseBudgetExceededError

    def mark_terminal(self, reason: str) -> None:
        with self._lock:
            if self._terminal_reason is None:
                self._terminal_reason = reason

    def expire(self) -> None:
        """Record an observed request timeout, including timeout-driven cancellation."""
        with self._lock:
            self._terminal_reason = "deadline"

    def cancel(self) -> None:
        self.mark_terminal("cancelled")

    def note_outcome(self, outcome: str) -> None:
        """Attach a safe public outcome code without changing terminal state."""
        with self._lock:
            self._outcome_hint = outcome

    def note_requested_range_days(self, count: int) -> None:
        """Record only an aggregate range size; never retain requested dates."""
        if count < 0:
            raise ValueError("Requested range day count cannot be negative")
        with self._lock:
            self._requested_range_days = max(self._requested_range_days, count)

    def note_truncation(self, reason: str) -> None:
        """Record the first safe, closed-vocabulary partial-result reason."""
        if reason not in {"page_items", "api_calls", "response_bytes"}:
            raise ValueError("Unsupported truncation reason")
        with self._lock:
            if self._truncation_reason is None or reason == "response_bytes":
                self._truncation_reason = reason

    def note_continuation(self) -> None:
        """Record ordinary cursor pagination without calling it a partial result."""
        with self._lock:
            self._continuation_emitted = True

    def mark_mutation_confirmed(self) -> None:
        """Remember that retry-facing failures must not replace a completed write."""
        with self._lock:
            self._mutation_outcome = "confirmed"

    def mark_mutation_uncertain(self) -> None:
        """Preserve mandatory reconciliation even when this request did not dispatch."""
        with self._lock:
            if self._mutation_outcome != "confirmed":
                self._mutation_outcome = "uncertain"

    def mark_mutation_definitely_failed(self) -> None:
        """Record that a classified rejection cannot have committed the mutation."""
        with self._lock:
            if self._mutation_outcome != "confirmed":
                self._mutation_outcome = "definitely_failed"

    def mark_mutation_dispatched(self) -> None:
        """Record that a write may still complete after this request ends."""
        with self._lock:
            self._raise_for_terminal(self._terminal_reason)
            if time.monotonic() >= self._deadline:
                self._terminal_reason = "deadline"
                raise RequestDeadlineExceededError
            self._mutation_dispatched = True
            self._mutation_outcome = "uncertain"


def _projected_collection_item_count(value: Any) -> int:
    """Count public collection members without counting their nested fields twice."""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, Mapping):
        return sum(_projected_collection_item_count(item) for item in value.values())
    return 0


def reserve_projected_response_items(
    surface: str,
    data: Any,
    policy_context: Mapping[str, object] | None = None,
) -> None:
    """Charge the actual fail-closed public collections to the request item budget."""
    budget = current_request_budget()
    if budget is None:
        return
    from .response_policy import project_surface_data

    projected = project_surface_data(surface, data, policy_context)
    count = _projected_collection_item_count(projected)
    budget.reserve_items(count or 1)


class BudgetedGarminReadClient:
    """Read facade that charges every dependency dispatch to one request."""

    def __init__(
        self,
        client: Any,
        budget: RequestBudget,
        *,
        client_provider: Callable[[], Any] | None = None,
    ):
        self._client = client
        self._client_provider = client_provider
        self._client_lock = threading.Lock()
        self.budget = budget

    @classmethod
    def lazy(
        cls,
        client_provider: Callable[[], Any],
        budget: RequestBudget,
    ) -> BudgetedGarminReadClient:
        """Create a facade that does no token, login, or network work until dispatch."""
        return cls(None, budget, client_provider=client_provider)

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self.budget.ensure_active()
                assert self._client_provider is not None
                self._client = self._client_provider()
                self.budget.ensure_active()
        return self._client

    def _dispatch(self, method_name: str, *args, **kwargs) -> Any:
        client = self._resolve_client()
        dispatch = getattr(client, "safe_call_with_preflight", None)
        if callable(dispatch):
            return dispatch(
                method_name,
                self.budget.ensure_active,
                *args,
                **kwargs,
            )
        # Test doubles and third-party facades without the production hook still
        # receive the latest possible cooperative checkpoint.
        self.budget.ensure_active()
        return client.safe_call(method_name, *args, **kwargs)

    def safe_call(self, method_name: str, *args, **kwargs) -> Any:
        """Budgeted synchronous compatibility boundary."""
        self.budget.ensure_active()
        self.budget.reserve_call()
        try:
            result = self._dispatch(method_name, *args, **kwargs)
        except GarminRateLimitError as exc:
            self.budget.mark_terminal("upstream_rate_limit")
            raise UpstreamRateLimitTerminatedError from exc
        except GarminAuthenticationError as exc:
            self.budget.mark_terminal("upstream_authentication")
            raise UpstreamAuthenticationTerminatedError from exc
        except GarminNotFoundError:
            raise
        except (GarminMethodNotAllowedError, GarminMethodUnavailableError):
            raise
        except GarminAPIError as exc:
            self.budget.mark_terminal("upstream_failure")
            raise UpstreamFailureTerminatedError from exc
        self.budget.ensure_active()
        return result

    async def call(self, method_name: str, *args, **kwargs) -> Any:
        """Run one blocking dependency call off the event loop with a deadline."""
        try:
            # Give already-requested cooperative cancellation a checkpoint before
            # submitting work to the executor. No event-loop yield occurs between
            # the explicit cancelling() check and executor submission below.
            await asyncio.sleep(0)
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                self.budget.cancel()
                raise asyncio.CancelledError
            self.budget.ensure_active()
            await _acquire_executor_slot(_GARMIN_READ_ADMISSION, self.budget)
            try:
                self.budget.reserve_call()
            except BaseException:
                _GARMIN_READ_ADMISSION.release()
                raise
            _concurrent_future, async_future = _submit_bounded(
                _GARMIN_READ_EXECUTOR,
                _GARMIN_READ_ADMISSION,
                partial(
                    self._dispatch,
                    method_name,
                    *args,
                    **kwargs,
                ),
            )
            async with asyncio.timeout(self.budget.remaining_seconds()):
                result = await asyncio.shield(async_future)
        except TimeoutError as exc:
            self.budget.expire()
            raise RequestDeadlineExceededError from exc
        except asyncio.CancelledError:
            self.budget.cancel()
            raise
        except GarminRateLimitError as exc:
            self.budget.mark_terminal("upstream_rate_limit")
            raise UpstreamRateLimitTerminatedError from exc
        except GarminAuthenticationError as exc:
            self.budget.mark_terminal("upstream_authentication")
            raise UpstreamAuthenticationTerminatedError from exc
        except GarminNotFoundError:
            raise
        except (GarminMethodNotAllowedError, GarminMethodUnavailableError):
            raise
        except GarminAPIError as exc:
            self.budget.mark_terminal("upstream_failure")
            raise UpstreamFailureTerminatedError from exc
        self.budget.ensure_active()
        return result


class BudgetedGarminMutationClient:
    """Mutation facade preserving policy and idempotency semantics."""

    def __init__(
        self,
        client: Any,
        budget: RequestBudget,
        *,
        client_provider: Callable[[], Any] | None = None,
    ):
        self._client = client
        self._client_provider = client_provider
        self._client_lock = threading.Lock()
        self._result_lock = threading.Lock()
        self._confirmed_result: Any = _UNSET_MUTATION_RESULT
        self.budget = budget

    @classmethod
    def lazy(
        cls,
        client_provider: Callable[[], Any],
        budget: RequestBudget,
    ) -> BudgetedGarminMutationClient:
        """Defer token/session work until the bounded mutation worker is admitted."""
        return cls(None, budget, client_provider=client_provider)

    def _resolve_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self.budget.ensure_active()
                assert self._client_provider is not None
                self._client = self._client_provider()
                self.budget.ensure_active()
        return self._client

    async def mutate(self, method_name: str, *args, **kwargs) -> Any:
        """Run one blocking write off-loop and quarantine an unconfirmed timeout."""
        await asyncio.sleep(0)
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            self.budget.cancel()
            raise asyncio.CancelledError
        self.budget.ensure_active()
        await _acquire_executor_slot(_GARMIN_MUTATION_ADMISSION, self.budget)
        concurrent_future, async_future = _submit_bounded(
            _GARMIN_MUTATION_EXECUTOR,
            _GARMIN_MUTATION_ADMISSION,
            partial(self._invoke, method_name, *args, **kwargs),
        )
        try:
            async with asyncio.timeout(self.budget.remaining_seconds()):
                result = await asyncio.shield(async_future)
        except TimeoutError as exc:
            self.budget.expire()
            confirmed, confirmed_result = self._confirmed_result_if_available()
            if confirmed:
                return confirmed_result
            if concurrent_future.cancel() or not self.budget.mutation_uncertain:
                raise RequestDeadlineExceededError from exc
            self.budget.note_outcome("GARMIN_UPSTREAM_UNAVAILABLE")
            raise GarminMutationOutcomeUnknownError(
                "The Garmin mutation was submitted but not confirmed before the request "
                "ended. Reconcile it before retrying with the same idempotency_key."
            ) from exc
        except asyncio.CancelledError:
            concurrent_future.cancel()
            self.budget.cancel()
            if self.budget.mutation_uncertain:
                self.budget.note_outcome("GARMIN_UPSTREAM_UNAVAILABLE")
            raise
        # Once a mutation returns successfully, never replace that confirmed result
        # with a later deadline error: doing so could invite an unsafe duplicate retry.
        return result

    def _invoke(self, method_name: str, *args, **kwargs) -> Any:
        """Dispatch with exact call charging and publish a confirmed remote result."""
        if "idempotency_key" in kwargs:
            try:
                validate_idempotency_key(kwargs["idempotency_key"])
            except GarminInvalidIdempotencyKeyError:
                self.budget.mark_mutation_definitely_failed()
                raise
        client = self._resolve_client()
        mutate_with_preflight = getattr(client, "mutate_with_preflight", None)
        if callable(mutate_with_preflight):
            try:
                result = mutate_with_preflight(
                    method_name,
                    *args,
                    **kwargs,
                    dispatch_preflight=self._dispatch_preflight,
                    reserve_items=self.budget.reserve_items,
                    on_committed=self._confirm_result,
                )
            except (GarminMutationOutcomeUnknownError, GarminMutationInProgressError):
                self.budget.mark_mutation_uncertain()
                raise
            except Exception:
                self.budget.mark_mutation_definitely_failed()
                raise
        else:
            # Isolated doubles do not expose the production boundary hook.
            self._dispatch_preflight(True)
            try:
                result = client.mutate(method_name, *args, **kwargs)
            except Exception:
                self.budget.mark_mutation_uncertain()
                raise
            self._confirm_result(result)
        return result

    def _dispatch_preflight(self, is_mutation: bool) -> None:
        """Charge each direct dependency call and mark only actual writes."""
        self.budget.reserve_call()
        if is_mutation:
            self.budget.mark_mutation_dispatched()

    def _confirm_result(self, result: Any) -> None:
        with self._result_lock:
            if self._confirmed_result is _UNSET_MUTATION_RESULT:
                self._confirmed_result = result
                self.budget.mark_mutation_confirmed()

    def _confirmed_result_if_available(self) -> tuple[bool, Any]:
        with self._result_lock:
            if self._confirmed_result is _UNSET_MUTATION_RESULT:
                return False, None
            return True, self._confirmed_result


_CURRENT_BUDGET: contextvars.ContextVar[RequestBudget | None] = contextvars.ContextVar(
    "garmin_request_budget",
    default=None,
)


def set_current_request_budget(
    budget: RequestBudget,
) -> contextvars.Token[RequestBudget | None]:
    return _CURRENT_BUDGET.set(budget)


def reset_current_request_budget(token: contextvars.Token[RequestBudget | None]) -> None:
    _CURRENT_BUDGET.reset(token)


def current_request_budget() -> RequestBudget | None:
    return _CURRENT_BUDGET.get()


@contextmanager
def request_budget_scope(surface: str):
    """Bind a budget for resources that do not traverse tool middleware."""
    budget = RequestBudget(surface, policy_for_surface(surface))
    token = set_current_request_budget(budget)
    outcome = "ok"
    try:
        yield budget
    except asyncio.CancelledError:
        budget.cancel()
        outcome = "cancelled"
        raise
    except BaseException as exc:
        outcome = getattr(exc, "public_code", "error")
        raise
    finally:
        log_budget_outcome(budget, budget.outcome_hint or outcome)
        reset_current_request_budget(token)


def log_budget_outcome(budget: RequestBudget, outcome: str) -> None:
    """Emit only aggregate, secret-safe request telemetry."""
    elapsed = budget.elapsed_seconds
    if elapsed < 0.1:
        duration_bucket = "lt_100ms"
    elif elapsed < 0.5:
        duration_bucket = "100_499ms"
    elif elapsed < 1:
        duration_bucket = "500_999ms"
    elif elapsed < 5:
        duration_bucket = "1_4s"
    elif elapsed < 15:
        duration_bucket = "5_14s"
    else:
        duration_bucket = "gte_15s"
    logger.info(
        "Garmin MCP budget surface=%s outcome=%s duration_bucket=%s calls=%d/%d "
        "items=%d response_bytes=%d requested_range_days=%d truncation=%s "
        "continuation=%s terminal=%s",
        budget.surface,
        outcome,
        duration_bucket,
        budget.calls_used,
        budget.policy.max_api_calls,
        budget.items_used,
        budget.response_bytes,
        budget.requested_range_days,
        budget.truncation_reason or "none",
        "true" if budget.continuation_emitted else "false",
        budget.terminal_reason or "none",
    )
