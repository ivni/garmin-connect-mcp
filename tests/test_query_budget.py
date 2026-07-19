"""Executable contracts for deterministic per-request resource budgets."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
from fastmcp.tools.base import ToolResult

from garmin_connect_mcp import query_budget as query_budget_module
from garmin_connect_mcp import server
from garmin_connect_mcp.client import (
    GarminClientWrapper,
    GarminMethodUnavailableError,
    GarminMutationClient,
    GarminMutationOutcomeUnknownError,
    GarminMutationPreDispatchError,
    GarminRateLimitError,
    GarminReadClient,
    MutationOperation,
    MutationRegistry,
)
from garmin_connect_mcp.pagination import (
    MAX_CONTINUATION_POSITION,
    decode_continuation_cursor,
    encode_continuation_cursor,
    paginate_date_range,
)
from garmin_connect_mcp.query_budget import (
    BUDGET_POLICIES,
    RANGE_SURFACE_ARGUMENTS,
    ApiCallBudgetExceededError,
    BudgetedGarminMutationClient,
    BudgetedGarminReadClient,
    InvalidContinuationCursorError,
    InvalidDateRangeError,
    InvalidPageSizeError,
    QueryBudgetPolicy,
    RangeTooLargeError,
    RequestBudget,
    RequestCancelledError,
    RequestDeadlineExceededError,
    ResponseBudgetExceededError,
    ResultItemBudgetExceededError,
    UpstreamAuthenticationTerminatedError,
    UpstreamRateLimitTerminatedError,
    log_budget_outcome,
    policy_for_surface,
    request_budget_scope,
    reserve_projected_response_items,
    reset_current_request_budget,
    set_current_request_budget,
    validate_date_range,
    validate_page_size,
)
from garmin_connect_mcp.response_builder import ResponseBuilder
from garmin_connect_mcp.time_utils import local_noon_timestamp, parse_time_range
from garmin_connect_mcp.tools.activities import get_activity_details, query_activities
from garmin_connect_mcp.tools.devices import query_devices
from garmin_connect_mcp.tools.health_wellness import (
    query_activity_metrics,
    query_health_summary,
    query_heart_rate_data,
    query_sleep_data,
)
from garmin_connect_mcp.tools.training import (
    analyze_training_period,
    get_performance_metrics,
    get_training_effect,
)
from garmin_connect_mcp.tools.weight import query_weight_data
from garmin_connect_mcp.tools.womens_health import query_womens_health


def _policy(
    *,
    max_range_days: int | None = 31,
    default_page_items: int = 7,
    max_page_items: int = 31,
    max_api_calls: int = 2,
    max_response_bytes: int = 4096,
    timeout_seconds: float = 1.0,
    partial_results_allowed: bool = False,
) -> QueryBudgetPolicy:
    return QueryBudgetPolicy(
        max_range_days=max_range_days,
        default_page_items=default_page_items,
        max_page_items=max_page_items,
        max_api_calls=max_api_calls,
        max_response_bytes=max_response_bytes,
        timeout_seconds=timeout_seconds,
        partial_results_allowed=partial_results_allowed,
    )


class RecordingRawClient:
    def __init__(self, result: object = None, error: Exception | None = None):
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.result = {} if result is None else result
        self.error = error

    def safe_call(self, method_name: str, *args: object, **kwargs: object) -> object:
        self.calls.append((method_name, args, kwargs))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_every_registered_surface_has_one_immutable_budget_policy():
    tools = await server.mcp.list_tools()
    resources = await server.mcp.list_resources()
    registered = {tool.name for tool in tools} | {str(resource.uri) for resource in resources}

    assert set(BUDGET_POLICIES) == registered
    with pytest.raises(TypeError):
        BUDGET_POLICIES["query_activities"] = _policy()  # type: ignore[index]


def test_every_inventoried_range_argument_exists_on_its_surface():
    assert set(RANGE_SURFACE_ARGUMENTS) <= set(BUDGET_POLICIES)
    for surface, argument_names in RANGE_SURFACE_ARGUMENTS.items():
        signature = inspect.signature(getattr(server, surface))
        assert set(argument_names) <= set(signature.parameters)
        assert policy_for_surface(surface).max_range_days is not None


def test_inclusive_range_boundaries_leap_day_and_future_dates():
    leap_policy = _policy(max_range_days=366)
    exact = validate_date_range("2024-01-01", "2024-12-31", policy=leap_policy)
    assert exact.day_count == 366
    assert tuple(exact.iter_dates())[:2] == ("2024-01-01", "2024-01-02")

    leap = validate_date_range("2024-02-28", "2024-03-01", policy=_policy(max_range_days=3))
    assert tuple(leap.iter_dates()) == ("2024-02-28", "2024-02-29", "2024-03-01")

    future = validate_date_range("2099-12-31", "2100-01-01", policy=_policy())
    assert future.day_count == 2

    with pytest.raises(RangeTooLargeError):
        validate_date_range("2024-01-01", "2025-01-01", policy=leap_policy)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (None, "2024-01-01"),
        ("2024-01-01", None),
        ("2024-01-02", "2024-01-01"),
        ("not-a-date", "2024-01-01"),
    ],
)
def test_invalid_ranges_are_rejected(start: str | None, end: str | None):
    with pytest.raises(InvalidDateRangeError):
        validate_date_range(start, end, policy=_policy())


def test_arithmetic_date_pagination_has_no_gaps_across_leap_day():
    policy = _policy(max_range_days=4, default_page_items=2, max_page_items=2)
    bounded = validate_date_range("2024-02-28", "2024-03-02", policy=policy)
    filters = {"start_date": bounded.start_iso, "end_date": bounded.end_iso}

    first, first_page, first_position = paginate_date_range(
        bounded,
        surface="query_sleep_data",
        filters=filters,
        policy=policy,
        cursor=None,
        requested_page_size=None,
    )
    second, second_page, second_position = paginate_date_range(
        bounded,
        surface="query_sleep_data",
        filters=filters,
        policy=policy,
        cursor=first_page["cursor"],
        requested_page_size=None,
    )

    assert first_position == 0
    assert second_position == 2
    assert first + second == (
        "2024-02-28",
        "2024-02-29",
        "2024-03-01",
        "2024-03-02",
    )
    assert second_page["has_more"] is False


def test_page_size_and_cursor_cannot_relax_server_limits():
    policy = _policy(default_page_items=2, max_page_items=3)
    assert validate_page_size(None, policy) == 2
    assert validate_page_size("3", policy) == 3
    with pytest.raises(InvalidPageSizeError):
        validate_page_size(4, policy)
    with pytest.raises(InvalidPageSizeError):
        validate_page_size(True, policy)

    filters = {"start_date": "2024-01-01", "end_date": "2024-01-10"}
    cursor = encode_continuation_cursor(
        surface="query_sleep_data",
        position=2,
        page_size=3,
        filters=filters,
    )
    assert decode_continuation_cursor(
        cursor,
        surface="query_sleep_data",
        filters=filters,
        policy=policy,
    ) == (2, 3)

    with pytest.raises(ValueError):
        decode_continuation_cursor(
            cursor,
            surface="query_heart_rate_data",
            filters=filters,
            policy=policy,
        )
    with pytest.raises(ValueError):
        decode_continuation_cursor(
            cursor,
            surface="query_sleep_data",
            filters={**filters, "end_date": "2024-01-11"},
            policy=policy,
        )

    raw = base64.urlsafe_b64decode(cursor.encode())
    payload = json.loads(raw)
    payload["policy_version"] = "stale"
    stale = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    with pytest.raises(ValueError):
        decode_continuation_cursor(
            stale,
            surface="query_sleep_data",
            filters=filters,
            policy=policy,
        )
    with pytest.raises(ValueError):
        decode_continuation_cursor(
            "malformed!",
            surface="query_sleep_data",
            filters=filters,
            policy=policy,
        )
    with pytest.raises(ValueError):
        decode_continuation_cursor(
            "A" * 1025,
            surface="query_sleep_data",
            filters=filters,
            policy=policy,
        )
    with pytest.raises(ValueError):
        encode_continuation_cursor(
            surface="query_sleep_data",
            position=1_000_001,
            page_size=1,
            filters=filters,
        )

    near_ceiling = encode_continuation_cursor(
        surface="query_sleep_data",
        position=MAX_CONTINUATION_POSITION - 51,
        page_size=50,
        filters=filters,
    )
    ceiling_policy = _policy(default_page_items=50, max_page_items=50)
    with pytest.raises(ValueError):
        decode_continuation_cursor(
            near_ceiling,
            surface="query_sleep_data",
            filters=filters,
            policy=ceiling_policy,
        )


def test_cursor_rejects_a_changed_explicit_page_size():
    policy = _policy(max_range_days=10, default_page_items=2, max_page_items=3)
    bounded = validate_date_range("2024-01-01", "2024-01-10", policy=policy)
    filters = {"start_date": bounded.start_iso, "end_date": bounded.end_iso}
    cursor = encode_continuation_cursor(
        surface="query_sleep_data",
        position=2,
        page_size=2,
        filters=filters,
    )
    with pytest.raises(InvalidContinuationCursorError):
        paginate_date_range(
            bounded,
            surface="query_sleep_data",
            filters=filters,
            policy=policy,
            cursor=cursor,
            requested_page_size=3,
        )


def test_call_budget_allows_n_and_refuses_n_plus_one_before_dispatch():
    raw = RecordingRawClient(result={"ok": True})
    budget = RequestBudget("test", _policy(max_api_calls=2))
    client = BudgetedGarminReadClient(raw, budget)

    assert client.safe_call("one") == {"ok": True}
    assert client.safe_call("two") == {"ok": True}
    with pytest.raises(ApiCallBudgetExceededError):
        client.safe_call("three")

    assert [call[0] for call in raw.calls] == ["one", "two"]
    assert budget.calls_used == 2


def test_rate_limit_is_terminal_and_prevents_another_raw_call():
    raw = RecordingRawClient(error=GarminRateLimitError())
    budget = RequestBudget("test", _policy(max_api_calls=2))
    client = BudgetedGarminReadClient(raw, budget)

    with pytest.raises(UpstreamRateLimitTerminatedError):
        client.safe_call("one")
    with pytest.raises(UpstreamRateLimitTerminatedError):
        client.safe_call("two")

    assert len(raw.calls) == 1
    assert budget.terminal_reason == "upstream_rate_limit"


@pytest.mark.asyncio
async def test_started_mutation_timeout_is_reported_as_unknown_not_safe_to_retry():
    class SlowMutationClient:
        def mutate(self, _method_name: str) -> str:
            time.sleep(0.1)
            return "confirmed"

    budget = RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=0.02))
    client = BudgetedGarminMutationClient(SlowMutationClient(), budget)

    started = time.monotonic()
    with pytest.raises(GarminMutationOutcomeUnknownError):
        await client.mutate("write")
    assert time.monotonic() - started < 0.08
    assert budget.calls_used == 1
    assert budget.mutation_dispatched is True
    assert budget.mutation_confirmed is False
    assert budget.outcome_hint == "GARMIN_UPSTREAM_UNAVAILABLE"


@pytest.mark.asyncio
async def test_external_mutation_cancellation_propagates_and_drains_worker():
    started = threading.Event()
    release = threading.Event()

    class BlockingMutationClient:
        def mutate(self, _method_name: str) -> str:
            started.set()
            release.wait(timeout=2)
            return "confirmed"

    budget = RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=1))
    client = BudgetedGarminMutationClient(BlockingMutationClient(), budget)
    task = asyncio.create_task(client.mutate("write"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert budget.mutation_dispatched is True
        assert budget.outcome_hint == "GARMIN_UPSTREAM_UNAVAILABLE"
    finally:
        release.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_mutation_executor_admission_is_bounded_before_submission():
    all_started = threading.Event()
    release = threading.Event()

    class BlockingMutationClient:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def mutate(self, _method_name: str) -> str:
            with self.lock:
                self.calls += 1
                if self.calls == 2:
                    all_started.set()
            release.wait(timeout=2)
            return "confirmed"

    raw = BlockingMutationClient()
    active = [
        BudgetedGarminMutationClient(
            raw,
            RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=1)),
        )
        for _ in range(2)
    ]
    tasks = [asyncio.create_task(client.mutate("write")) for client in active]
    try:
        assert await asyncio.to_thread(all_started.wait, 1)
        refused = BudgetedGarminMutationClient(
            raw,
            RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=0.03)),
        )
        with pytest.raises(RequestDeadlineExceededError):
            await refused.mutate("write")
        assert raw.calls == 2
    finally:
        release.set()
    assert await asyncio.gather(*tasks) == ["confirmed", "confirmed"]


@pytest.mark.asyncio
async def test_expired_mutation_waiting_on_journal_never_reaches_remote_dispatch():
    journal_entered = threading.Event()
    release_journal = threading.Event()

    class BlockingJournal:
        def __init__(self):
            self.records: dict[str, dict[str, str]] = {}

        @contextmanager
        def mutation_transaction(self):
            journal_entered.set()
            release_journal.wait(timeout=2)
            yield

        def update_mutation_records(self, update):
            result, _changed = update(self.records)
            return result

    class Dependency:
        def __init__(self):
            self.calls = 0

        def write(self) -> object:
            self.calls += 1
            return {"success": True}

    dependency = Dependency()
    operation = MutationOperation("test.write", "write", "check the written value")
    production_client = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        operation,
        MutationRegistry(journal=BlockingJournal()),
    )
    budget = RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=0.05))
    client = BudgetedGarminMutationClient(production_client, budget)
    try:
        with pytest.raises(RequestDeadlineExceededError):
            await client.mutate("write", idempotency_key="journal-wait-key")
        assert journal_entered.is_set()
        assert budget.mutation_dispatched is False
        assert budget.calls_used == 0
    finally:
        release_journal.set()

    await asyncio.sleep(0.05)
    assert dependency.calls == 0


@pytest.mark.asyncio
async def test_missing_mutation_method_is_rejected_before_dispatch_or_call_charge():
    class Dependency:
        pass

    after_calls = 0

    def after_call() -> None:
        nonlocal after_calls
        after_calls += 1

    budget = RequestBudget("write", _policy(max_api_calls=1))
    production = GarminMutationClient(
        GarminClientWrapper(Dependency(), after_call=after_call),  # type: ignore[arg-type]
        MutationOperation("test.write", "write", "check the value"),
        MutationRegistry(),
    )

    with pytest.raises(GarminMethodUnavailableError):
        await BudgetedGarminMutationClient(production, budget).mutate(
            "write",
            idempotency_key="missing-method-key",
        )

    assert budget.calls_used == 0
    assert budget.mutation_dispatched is False
    assert budget.mutation_confirmed is False
    assert after_calls == 0


def test_rejected_dispatch_preflight_does_not_run_session_after_call():
    class Dependency:
        def __init__(self):
            self.calls = 0

        def probe(self) -> object:
            self.calls += 1
            return {}

    dependency = Dependency()
    after_calls = 0

    def after_call() -> None:
        nonlocal after_calls
        after_calls += 1

    wrapper = GarminClientWrapper(
        dependency,  # type: ignore[arg-type]
        after_call=after_call,
    )

    with pytest.raises(RequestDeadlineExceededError):
        wrapper.safe_call_with_preflight(
            "probe",
            lambda: (_ for _ in ()).throw(RequestDeadlineExceededError()),
        )

    assert dependency.calls == 0
    assert after_calls == 0


@pytest.mark.asyncio
async def test_remote_success_stays_unknown_until_durable_ledger_finalization():
    finalization_started = threading.Event()
    release_finalization = threading.Event()

    class BlockingFinalizeJournal:
        def __init__(self):
            self.records: dict[str, dict[str, str]] = {}

        @contextmanager
        def mutation_transaction(self):
            yield

        def update_mutation_records(self, update):
            result, _changed = update(self.records)
            if any(record["state"] == "succeeded" for record in self.records.values()):
                finalization_started.set()
                release_finalization.wait(timeout=2)
            return result

    class Dependency:
        def write(self) -> dict[str, bool]:
            return {"success": True}

    budget = RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=0.05))
    production = GarminMutationClient(
        GarminClientWrapper(Dependency()),  # type: ignore[arg-type]
        MutationOperation("test.write", "write", "check the value"),
        MutationRegistry(journal=BlockingFinalizeJournal()),
    )
    try:
        with pytest.raises(GarminMutationOutcomeUnknownError):
            await BudgetedGarminMutationClient(production, budget).mutate(
                "write",
                idempotency_key="delayed-finalize-key",
            )
        assert finalization_started.is_set()
        assert budget.mutation_confirmed is False
    finally:
        release_finalization.set()

    await asyncio.sleep(0.05)
    assert budget.mutation_confirmed is True


@pytest.mark.asyncio
async def test_ledger_finalization_failure_never_publishes_confirmed_success():
    class FailingFinalizeJournal:
        def __init__(self):
            self.records: dict[str, dict[str, str]] = {}

        @contextmanager
        def mutation_transaction(self):
            yield

        def update_mutation_records(self, update):
            result, _changed = update(self.records)
            if any(record["state"] == "succeeded" for record in self.records.values()):
                raise OSError("ledger unavailable")
            return result

    class Dependency:
        def write(self) -> dict[str, bool]:
            return {"success": True}

    budget = RequestBudget("write", _policy(max_api_calls=1))
    production = GarminMutationClient(
        GarminClientWrapper(Dependency()),  # type: ignore[arg-type]
        MutationOperation("test.write", "write", "check the value"),
        MutationRegistry(journal=FailingFinalizeJournal()),
    )

    with pytest.raises(GarminMutationOutcomeUnknownError):
        await BudgetedGarminMutationClient(production, budget).mutate(
            "write",
            idempotency_key="failed-finalize-key",
        )

    assert budget.mutation_dispatched is True
    assert budget.mutation_confirmed is False


@pytest.mark.asyncio
async def test_worker_confirmation_wins_over_delayed_event_loop_delivery(monkeypatch):
    pending_deliveries: list[asyncio.Future[Any]] = []

    def delayed_submit(executor, admission, invoke):
        concurrent_future = executor.submit(invoke)
        concurrent_future.add_done_callback(lambda _future: admission.release())
        async_future = asyncio.get_running_loop().create_future()
        pending_deliveries.append(async_future)
        return concurrent_future, async_future

    monkeypatch.setattr(query_budget_module, "_submit_bounded", delayed_submit)

    class ImmediateMutationClient:
        def mutate(self, _method_name: str) -> dict[str, bool]:
            return {"success": True}

    budget = RequestBudget("write", _policy(max_api_calls=1, timeout_seconds=0.03))
    client = BudgetedGarminMutationClient(ImmediateMutationClient(), budget)
    try:
        assert await client.mutate("write") == {"success": True}
    finally:
        for future in pending_deliveries:
            future.cancel()

    assert budget.mutation_confirmed is True


@pytest.mark.asyncio
async def test_durable_deduplication_charges_zero_remote_calls():
    class Dependency:
        def __init__(self):
            self.calls = 0

        def write(self) -> dict[str, bool]:
            self.calls += 1
            return {"success": True}

    dependency = Dependency()
    production = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        MutationOperation("test.write", "write", "check the value"),
        MutationRegistry(),
    )
    first_budget = RequestBudget("write", _policy(max_api_calls=1))
    second_budget = RequestBudget("write", _policy(max_api_calls=1))

    await BudgetedGarminMutationClient(production, first_budget).mutate(
        "write",
        idempotency_key="deduplicated-key",
    )
    replay = await BudgetedGarminMutationClient(production, second_budget).mutate(
        "write",
        idempotency_key="deduplicated-key",
    )

    assert replay == {"success": True}
    assert dependency.calls == 1
    assert first_budget.calls_used == 1
    assert second_budget.calls_used == 0
    assert second_budget.mutation_confirmed is True


@pytest.mark.asyncio
async def test_date_wide_weight_delete_charges_each_direct_dependency_call():
    class Dependency:
        def __init__(self):
            self.deleted: list[tuple[str, str]] = []

        def get_daily_weigh_ins(self, _date: str) -> dict[str, object]:
            return {"dateWeightList": [{"samplePk": value} for value in ("1", "2", "3")]}

        def delete_weigh_in(self, sample_id: str, date: str) -> None:
            self.deleted.append((sample_id, date))

    dependency = Dependency()
    operation = MutationOperation(
        "weight.delete",
        "delete_weigh_ins",
        "reconcile the target date",
        frozenset({"delete_weigh_ins", "delete_weigh_in"}),
    )
    production = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        operation,
        MutationRegistry(),
    )
    budget = RequestBudget(
        "delete_weight_entries",
        _policy(
            max_range_days=None,
            default_page_items=10,
            max_page_items=10,
            max_api_calls=11,
        ),
    )

    result = await BudgetedGarminMutationClient(production, budget).mutate(
        "delete_weigh_ins",
        "2026-07-19",
        True,
        idempotency_key="bounded-delete-key",
    )

    assert result == 3
    assert dependency.deleted == [
        ("1", "2026-07-19"),
        ("2", "2026-07-19"),
        ("3", "2026-07-19"),
    ]
    assert budget.calls_used == 4
    assert budget.items_used == 3
    assert budget.mutation_confirmed is True


@pytest.mark.asyncio
async def test_date_wide_weight_delete_refuses_too_many_items_before_first_delete():
    class Dependency:
        def __init__(self):
            self.delete_calls = 0

        def get_daily_weigh_ins(self, _date: str) -> dict[str, object]:
            return {"dateWeightList": [{"samplePk": str(value)} for value in range(11)]}

        def delete_weigh_in(self, _sample_id: str, _date: str) -> None:
            self.delete_calls += 1

    dependency = Dependency()
    operation = MutationOperation(
        "weight.delete",
        "delete_weigh_ins",
        "reconcile the target date",
    )
    production = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        operation,
        MutationRegistry(),
    )
    budget = RequestBudget(
        "delete_weight_entries",
        _policy(
            max_range_days=None,
            default_page_items=10,
            max_page_items=10,
            max_api_calls=11,
        ),
    )

    with pytest.raises(ResultItemBudgetExceededError):
        await BudgetedGarminMutationClient(production, budget).mutate(
            "delete_weigh_ins",
            "2026-07-19",
            True,
            idempotency_key="oversized-delete-key",
        )

    assert dependency.delete_calls == 0
    assert budget.mutation_dispatched is False


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [{"dateWeightList": [{}]}, RuntimeError("lookup failed")])
async def test_pre_delete_lookup_failure_clears_reservation_for_safe_same_key_retry(first_result):
    class Dependency:
        def __init__(self):
            self.lookups = 0

        def get_daily_weigh_ins(self, _date: str) -> dict[str, object]:
            self.lookups += 1
            if self.lookups == 1:
                if isinstance(first_result, Exception):
                    raise first_result
                return first_result
            return {"dateWeightList": []}

        def delete_weigh_in(self, _sample_id: str, _date: str) -> None:
            raise AssertionError("No delete should be dispatched")

    dependency = Dependency()
    production = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        MutationOperation("weight.delete", "delete_weigh_ins", "check the date"),
        MutationRegistry(),
    )

    first_budget = RequestBudget(
        "delete_weight_entries", policy_for_surface("delete_weight_entries")
    )
    with pytest.raises(GarminMutationPreDispatchError):
        await BudgetedGarminMutationClient(production, first_budget).mutate(
            "delete_weigh_ins",
            "2026-07-19",
            True,
            idempotency_key="lookup-failure-key",
        )
    assert first_budget.mutation_dispatched is False
    assert first_budget.calls_used == 1

    second_budget = RequestBudget(
        "delete_weight_entries",
        policy_for_surface("delete_weight_entries"),
    )
    assert (
        await BudgetedGarminMutationClient(production, second_budget).mutate(
            "delete_weigh_ins",
            "2026-07-19",
            True,
            idempotency_key="lookup-failure-key",
        )
        is None
    )
    assert dependency.lookups == 2


@pytest.mark.asyncio
async def test_cancelling_date_wide_delete_prevents_any_later_delete_dispatch():
    first_delete_started = threading.Event()
    release_first_delete = threading.Event()

    class Dependency:
        def __init__(self):
            self.deleted: list[str] = []

        def get_daily_weigh_ins(self, _date: str) -> dict[str, object]:
            return {"dateWeightList": [{"samplePk": "1"}, {"samplePk": "2"}]}

        def delete_weigh_in(self, sample_id: str, _date: str) -> None:
            self.deleted.append(sample_id)
            first_delete_started.set()
            release_first_delete.wait(timeout=2)

    dependency = Dependency()
    operation = MutationOperation(
        "weight.delete",
        "delete_weigh_ins",
        "reconcile the target date",
    )
    production = GarminMutationClient(
        GarminClientWrapper(dependency),  # type: ignore[arg-type]
        operation,
        MutationRegistry(),
    )
    budget = RequestBudget(
        "delete_weight_entries",
        _policy(
            max_range_days=None,
            default_page_items=10,
            max_page_items=10,
            max_api_calls=11,
        ),
    )
    task = asyncio.create_task(
        BudgetedGarminMutationClient(production, budget).mutate(
            "delete_weigh_ins",
            "2026-07-19",
            True,
            idempotency_key="cancelled-delete-key",
        )
    )
    try:
        assert await asyncio.to_thread(first_delete_started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_first_delete.set()

    await asyncio.sleep(0.1)
    assert dependency.deleted == ["1"]
    assert budget.terminal_reason == "cancelled"
    assert budget.mutation_confirmed is False


@pytest.mark.asyncio
async def test_confirmed_mutation_result_is_not_replaced_by_response_budget_error(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")

    class MutationClient:
        def mutate(self, _method_name: str) -> dict[str, bool]:
            return {"success": True}

    budget = RequestBudget(
        "upload_workout",
        policy_for_surface("upload_workout"),
    )
    client = BudgetedGarminMutationClient(MutationClient(), budget)
    assert await client.mutate("upload_workout") == {"success": True}

    token = set_current_request_budget(budget)
    try:
        response = ResponseBuilder.build_response(
            data={"result": {"success": True}},
            analysis={"insights": ["x" * (80 * 1024)]},
            surface="upload_workout",
        )
        log_budget_outcome(budget, budget.outcome_hint or "ok")
    finally:
        reset_current_request_budget(token)

    payload = json.loads(response)
    assert "error" not in payload
    assert payload["metadata"]["mutation_confirmed"] is True
    assert payload["metadata"]["result_omitted"] == "response_budget"
    assert len(response.encode("utf-8")) <= budget.policy.max_response_bytes
    assert "outcome=confirmed" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_parallel_requests_have_independent_call_budgets():
    raw = RecordingRawClient(result="ok")
    first = BudgetedGarminReadClient(raw, RequestBudget("first", _policy(max_api_calls=1)))
    second = BudgetedGarminReadClient(raw, RequestBudget("second", _policy(max_api_calls=1)))

    assert await asyncio.gather(first.call("one"), second.call("two")) == ["ok", "ok"]
    assert len(raw.calls) == 2


@pytest.mark.asyncio
async def test_read_executor_admission_never_builds_an_unbounded_queue():
    release = threading.Event()

    class SaturatedRawClient:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def safe_call(self, _method_name: str) -> object:
            with self.lock:
                self.calls += 1
            release.wait(timeout=2)
            return {}

    raw = SaturatedRawClient()
    clients = [
        BudgetedGarminReadClient(
            raw,
            RequestBudget("read", _policy(max_api_calls=1, timeout_seconds=0.05)),
        )
        for _ in range(12)
    ]
    tasks = [asyncio.create_task(client.call("read")) for client in clients]
    try:
        await asyncio.sleep(0.1)
        assert raw.calls <= 8
    finally:
        release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, RequestDeadlineExceededError) for result in results)


class BlockingRawClient:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def safe_call(self, _method_name: str, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return {}


@pytest.mark.asyncio
async def test_cancellation_checkpoint_prevents_the_first_executor_submission():
    raw = BlockingRawClient()
    budget = RequestBudget("test", _policy(max_api_calls=1))
    client = BudgetedGarminReadClient(raw, budget)
    task = asyncio.create_task(client.call("must-not-start"))

    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert raw.calls == 0
    assert budget.calls_used == 0
    assert budget.terminal_reason == "cancelled"


@pytest.mark.asyncio
async def test_cancelled_call_waiting_on_session_lock_never_reaches_dependency():
    class Dependency:
        def __init__(self):
            self.calls = 0

        def probe(self) -> object:
            self.calls += 1
            return {}

    dependency = Dependency()
    shared_lock = threading.RLock()
    shared_lock.acquire()
    raw_wrapper = GarminClientWrapper(dependency, lock=shared_lock)  # type: ignore[arg-type]
    read_client = GarminReadClient(raw_wrapper, frozenset({"probe"}))
    budget = RequestBudget("test", _policy(max_api_calls=1, timeout_seconds=1))
    client = BudgetedGarminReadClient(read_client, budget)
    task = asyncio.create_task(client.call("probe"))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        shared_lock.release()

    await asyncio.sleep(0.05)
    assert dependency.calls == 0
    assert budget.terminal_reason == "cancelled"


@pytest.mark.asyncio
async def test_expired_preflight_waiting_on_session_lock_keeps_deadline_error_type():
    class Dependency:
        def probe(self) -> object:
            raise AssertionError("Expired preflight must prevent dependency dispatch")

    shared_lock = threading.RLock()
    shared_lock.acquire()
    wrapper = GarminClientWrapper(Dependency(), lock=shared_lock)  # type: ignore[arg-type]
    read_client = GarminReadClient(wrapper, frozenset({"probe"}))
    budget = RequestBudget("test", _policy(max_api_calls=1, timeout_seconds=1))
    call = asyncio.create_task(
        asyncio.to_thread(
            read_client.safe_call_with_preflight,
            "probe",
            budget.ensure_active,
        )
    )
    try:
        await asyncio.sleep(0.05)
        budget.expire()
    finally:
        shared_lock.release()

    with pytest.raises(RequestDeadlineExceededError):
        await call


@pytest.mark.asyncio
async def test_cancellation_during_blocking_call_prevents_the_next_call():
    raw = BlockingRawClient()
    budget = RequestBudget("test", _policy(max_api_calls=2, timeout_seconds=1))
    client = BudgetedGarminReadClient(raw, budget)
    task = asyncio.create_task(client.call("blocking"))
    try:
        assert await asyncio.to_thread(raw.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RequestCancelledError):
            await client.call("must-not-start")
        assert raw.calls == 1
        assert budget.terminal_reason == "cancelled"
    finally:
        raw.release.set()


@pytest.mark.asyncio
async def test_deadline_during_blocking_call_prevents_the_next_call():
    raw = BlockingRawClient()
    budget = RequestBudget("test", _policy(max_api_calls=2, timeout_seconds=0.05))
    client = BudgetedGarminReadClient(raw, budget)
    try:
        with pytest.raises(RequestDeadlineExceededError):
            await client.call("blocking")
        with pytest.raises(RequestDeadlineExceededError):
            await client.call("must-not-start")
        assert raw.calls == 1
        assert budget.terminal_reason == "deadline"
    finally:
        raw.release.set()


def test_serialized_response_budget_counts_exact_utf8_bytes():
    policy = _policy(max_response_bytes=4096)
    exact = RequestBudget("test", policy)
    exact.record_response_size(4096)
    assert exact.response_bytes == 4096

    over = RequestBudget("test", policy)
    with pytest.raises(ResponseBudgetExceededError):
        over.record_response_size(4097)

    response_budget = RequestBudget("test", policy)
    token = set_current_request_budget(response_budget)
    try:
        response = ResponseBuilder.build_response({"value": "é" * 3000})
    finally:
        reset_current_request_budget(token)
    payload = json.loads(response)
    assert payload["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert len(response.encode("utf-8")) <= policy.max_response_bytes


def test_final_size_supports_explicit_tool_results_without_fastmcp_metadata():
    result = ToolResult(content="bounded")

    assert ResponseBuilder.serialized_result_size(result, "query_workouts") > len(b"bounded")


def test_collection_byte_budget_returns_a_cursor_to_the_first_omitted_item():
    policy = _policy(
        default_page_items=3,
        max_page_items=3,
        max_response_bytes=4096,
        partial_results_allowed=True,
    )
    budget = RequestBudget("test", policy)
    token = set_current_request_budget(budget)
    try:
        response = ResponseBuilder.build_bounded_collection_response(
            items=[
                {"activityName": "x" * 700},
                {"activityName": "y" * 700},
                {"activityName": "z" * 700},
            ],
            data_factory=lambda values: {"activities": values},
            metadata_factory=lambda _count: {},
            pagination={"cursor": None, "has_more": False, "limit": 3, "returned": 3},
            cursor_factory=lambda count: f"cursor-{count}",
            surface="query_activities",
        )
    finally:
        reset_current_request_budget(token)

    payload = json.loads(response)
    assert payload["data"]["activities"] == [
        {"activityName": "x" * 700},
        {"activityName": "y" * 700},
    ]
    assert payload["pagination"] == {
        "cursor": "cursor-2",
        "has_more": True,
        "limit": 3,
        "returned": 2,
        "partial": True,
        "truncation_reason": "response_bytes",
    }
    assert budget.items_used == 2
    assert len(response.encode("utf-8")) <= policy.max_response_bytes


def test_one_oversized_collection_item_returns_a_stable_error():
    policy = _policy(
        max_response_bytes=4096,
        partial_results_allowed=True,
    )
    budget = RequestBudget("test", policy)
    token = set_current_request_budget(budget)
    try:
        response = ResponseBuilder.build_bounded_collection_response(
            items=[{"activityName": "é" * 3000}],
            data_factory=lambda values: {"activities": values},
            metadata_factory=lambda _count: {},
            pagination={"cursor": None, "has_more": False, "limit": 1, "returned": 1},
            cursor_factory=lambda count: f"cursor-{count}",
            surface="query_activities",
        )
    finally:
        reset_current_request_budget(token)

    assert json.loads(response)["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert len(response.encode("utf-8")) <= policy.max_response_bytes


@pytest.mark.asyncio
async def test_daily_fanout_stops_before_the_call_after_a_byte_boundary():
    raw = RecordingRawClient(result={"dailySleepDTO": {"sleepScoreInsight": "x" * 800}})
    policy = _policy(
        max_range_days=31,
        default_page_items=3,
        max_page_items=3,
        max_api_calls=10,
        max_response_bytes=4096,
        partial_results_allowed=True,
    )
    budget = RequestBudget("query_sleep_data", policy)
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, key: str) -> BudgetedGarminReadClient:
            assert key == "client"
            return client

    token = set_current_request_budget(budget)
    try:
        response = await query_sleep_data(
            start_date="2024-01-01",
            end_date="2024-01-03",
            limit=3,
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    payload = json.loads(response)
    assert len(raw.calls) == 2
    assert payload["pagination"]["returned"] == 1
    assert payload["pagination"]["partial"] is True
    assert payload["pagination"]["truncation_reason"] == "response_bytes"
    assert budget.items_used == 1


@pytest.mark.asyncio
async def test_range_backed_metrics_page_data_matches_cursor_window():
    raw = RecordingRawClient(result={"value": "bounded"})
    policy = policy_for_surface("query_activity_metrics")
    budget = RequestBudget("query_activity_metrics", policy)
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, key: str) -> BudgetedGarminReadClient:
            assert key == "client"
            return client

    token = set_current_request_budget(budget)
    try:
        response = await query_activity_metrics(
            start_date="2024-01-01",
            end_date="2024-01-03",
            metrics="steps,blood_pressure,body_composition",
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    payload = json.loads(response)
    assert payload["metadata"]["start_date"] == "2024-01-01"
    assert payload["metadata"]["end_date"] == "2024-01-01"
    assert payload["pagination"]["returned"] == 1
    assert payload["pagination"]["has_more"] is True
    assert raw.calls == [
        ("get_steps_data", ("2024-01-01",), {}),
        ("get_blood_pressure", ("2024-01-01", "2024-01-01"), {}),
        ("get_body_composition", ("2024-01-01", "2024-01-01"), {}),
    ]


def test_resource_scope_logs_outcome_range_and_truncation_without_values(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    with request_budget_scope("garmin://health/today") as budget:
        budget.note_requested_range_days(31)
        budget.note_truncation("response_bytes")
        ResponseBuilder.build_exception_response(UpstreamAuthenticationTerminatedError())

    message = caplog.messages[-1]
    assert "outcome=AUTH_REQUIRED" in message
    assert "requested_range_days=31" in message
    assert "truncation=response_bytes" in message
    assert "duration_bucket=" in message
    assert "duration_ms=" not in message
    assert "CANARY_SECRET" not in message


def test_rejected_oversized_range_still_logs_aggregate_requested_days(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    with pytest.raises(RangeTooLargeError):
        with request_budget_scope("query_sleep_data"):
            validate_date_range(
                "2024-01-01",
                "2024-02-01",
                policy=policy_for_surface("query_sleep_data"),
            )

    message = caplog.messages[-1]
    assert "outcome=RANGE_TOO_LARGE" in message
    assert "requested_range_days=32" in message


@pytest.mark.parametrize(
    ("surface", "field"),
    [
        ("query_devices", "devices"),
        ("get_performance_metrics", "hill_score"),
        ("get_training_effect", "progress_summary"),
        ("query_weight_data", "weigh_ins"),
        ("query_womens_health", "menstrual_calendar"),
    ],
)
def test_unpaged_range_surfaces_reject_projected_collection_over_item_ceiling(
    surface: str,
    field: str,
):
    policy = policy_for_surface(surface)
    budget = RequestBudget(surface, policy)
    token = set_current_request_budget(budget)
    try:
        with pytest.raises(ResultItemBudgetExceededError):
            reserve_projected_response_items(
                surface,
                {field: [{} for _ in range(policy.max_page_items + 1)]},
            )
    finally:
        reset_current_request_budget(token)

    assert budget.items_used == 0
    assert budget.terminal_reason == "items"


def test_resource_scope_marks_async_cancellation_terminal(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    with pytest.raises(asyncio.CancelledError):
        with request_budget_scope("garmin://health/today"):
            raise asyncio.CancelledError

    assert "outcome=cancelled" in caplog.messages[-1]
    assert "terminal=cancelled" in caplog.messages[-1]


class NoLookupContext:
    def __init__(self):
        self.lookups = 0

    async def get_state(self, _key: str) -> Any:
        self.lookups += 1
        raise AssertionError("Client lookup must not happen after failed preflight")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "arguments"),
    [
        (
            query_activities,
            {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        ),
        (
            query_health_summary,
            {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        ),
        (
            query_sleep_data,
            {"start_date": "2024-01-01", "end_date": "2024-02-01"},
        ),
        (
            query_heart_rate_data,
            {"start_date": "2024-01-01", "end_date": "2024-02-01"},
        ),
        (
            query_activity_metrics,
            {"start_date": "2024-01-01", "end_date": "2024-02-01"},
        ),
        (
            query_devices,
            {
                "device_id": 1,
                "include_solar_data": True,
                "solar_start_date": "2024-01-01",
                "solar_end_date": "2024-02-01",
            },
        ),
        (
            analyze_training_period,
            {"period": "2024-01-01:2025-01-01"},
        ),
        (
            get_performance_metrics,
            {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        ),
        (
            get_training_effect,
            {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        ),
        (
            query_weight_data,
            {"start_date": "2024-01-01", "end_date": "2025-01-01"},
        ),
        (
            query_womens_health,
            {
                "data_type": "menstrual",
                "start_date": "2024-01-01",
                "end_date": "2025-01-01",
            },
        ),
    ],
)
async def test_every_range_surface_rejects_max_plus_one_before_client_lookup(
    function: Any,
    arguments: dict[str, Any],
):
    context = NoLookupContext()

    result = await function(ctx=context, **arguments)

    assert json.loads(result)["error"]["code"] == "RANGE_TOO_LARGE"
    assert context.lookups == 0


@pytest.mark.asyncio
async def test_activity_cursor_is_validated_before_client_lookup():
    context = NoLookupContext()

    result = await query_activities(
        start_date="2024-01-01",
        end_date="2024-01-31",
        cursor="malformed!",
        ctx=context,  # type: ignore[arg-type]
    )

    assert json.loads(result)["error"]["code"] == "INVALID_CONTINUATION_CURSOR"
    assert context.lookups == 0


@pytest.mark.asyncio
async def test_activity_scan_cursor_capacity_is_validated_before_client_lookup():
    context = NoLookupContext()
    cursor = encode_continuation_cursor(
        surface="query_activities",
        position=998_800,
        page_size=20,
        filters={
            "start_date": "2024-01-01",
            "end_date": "2024-01-31",
            "activity_type": "",
            "unit": "metric",
            "include_location": False,
        },
    )

    result = await query_activities(
        start_date="2024-01-01",
        end_date="2024-01-31",
        cursor=cursor,
        ctx=context,  # type: ignore[arg-type]
    )

    assert json.loads(result)["error"]["code"] == "INVALID_CONTINUATION_CURSOR"
    assert context.lookups == 0


@pytest.mark.asyncio
async def test_metrics_call_fanout_is_rejected_before_client_lookup():
    context = NoLookupContext()
    budget = RequestBudget(
        "query_activity_metrics",
        policy_for_surface("query_activity_metrics"),
    )
    token = set_current_request_budget(budget)
    try:
        result = await query_activity_metrics(
            start_date="2024-01-01",
            end_date="2024-01-31",
            limit=31,
            metrics="steps,stress,respiration,spo2,floors,hydration",
            ctx=context,  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    assert json.loads(result)["error"]["code"] == "API_CALL_BUDGET_EXCEEDED"
    assert context.lookups == 0


@pytest.mark.asyncio
async def test_range_backed_metrics_reject_boolean_page_size_before_client_lookup():
    context = NoLookupContext()

    result = await query_activity_metrics(
        start_date="2024-01-01",
        end_date="2024-01-02",
        metrics="blood_pressure",
        limit=True,  # type: ignore[arg-type]
        ctx=context,  # type: ignore[arg-type]
    )

    assert json.loads(result)["error"]["code"] == "INVALID_PAGE_SIZE"
    assert context.lookups == 0


@pytest.mark.asyncio
async def test_local_date_scan_does_not_stop_before_a_later_matching_row():
    raw = RecordingRawClient(
        result=[
            {"activityId": 1, "startTimeLocal": "2024-01-01T23:30:00"},
            {"activityId": 2, "startTimeLocal": "2024-01-02T00:15:00"},
        ]
    )
    budget = RequestBudget("query_activities", policy_for_surface("query_activities"))
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, _key: str) -> BudgetedGarminReadClient:
            return client

    token = set_current_request_budget(budget)
    try:
        response = await query_activities(
            start_date="2024-01-02",
            end_date="2024-01-02",
            limit=5,
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    activities = json.loads(response)["data"]["activities"]
    assert [activity["activityId"] for activity in activities] == [2]


@pytest.mark.asyncio
async def test_activity_page_limit_is_recorded_in_budget_telemetry(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    raw = RecordingRawClient(
        result=[{"activityId": item, "startTimeLocal": "2024-01-02T08:00:00"} for item in range(21)]
    )
    budget = RequestBudget("query_activities", policy_for_surface("query_activities"))
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, _key: str) -> BudgetedGarminReadClient:
            return client

    token = set_current_request_budget(budget)
    try:
        response = await query_activities(
            limit=20,
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
        log_budget_outcome(budget, "ok")
    finally:
        reset_current_request_budget(token)

    assert json.loads(response)["pagination"]["truncation_reason"] == "page_limit"
    assert budget.truncation_reason == "page_items"
    assert "truncation=page_items" in caplog.messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("activity_id", [42, None])
async def test_single_activity_paths_charge_one_projected_result_item(activity_id):
    raw = RecordingRawClient(
        result={
            "activityId": 42,
            "activityName": "Run",
            "startTimeLocal": "2026-07-19T08:00:00",
        }
    )
    budget = RequestBudget("query_activities", policy_for_surface("query_activities"))
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, _key: str) -> BudgetedGarminReadClient:
            return client

    token = set_current_request_budget(budget)
    try:
        response = await query_activities(
            activity_id=activity_id,
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    assert json.loads(response)["data"]["activity"]["activityId"] == 42
    assert budget.items_used == 1


@pytest.mark.asyncio
async def test_terminal_rate_limit_in_optional_activity_data_is_not_swallowed():
    class ActivityRawClient(RecordingRawClient):
        def safe_call(self, method_name: str, *args: object, **kwargs: object) -> object:
            self.calls.append((method_name, args, kwargs))
            if method_name == "get_activity":
                return {"activityId": 42, "activityName": "Run"}
            raise GarminRateLimitError

    raw = ActivityRawClient()
    budget = RequestBudget("get_activity_details", policy_for_surface("get_activity_details"))
    client = BudgetedGarminReadClient(raw, budget)

    class ClientContext:
        async def get_state(self, _key: str) -> BudgetedGarminReadClient:
            return client

    token = set_current_request_budget(budget)
    try:
        response = await get_activity_details(
            42,
            include_weather=True,
            ctx=ClientContext(),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    assert json.loads(response)["error"]["code"] == "RATE_LIMITED"
    assert [call[0] for call in raw.calls] == ["get_activity", "get_activity_splits"]


def test_noon_normalization_keeps_calendar_date_at_dst_boundaries():
    assert local_noon_timestamp("2024-03-31") == "2024-03-31T12:00:00"
    assert local_noon_timestamp("2024-10-27") == "2024-10-27T12:00:00"
    assert datetime.fromisoformat(local_noon_timestamp("2024-02-29")).hour == 12


def test_relative_period_names_their_inclusive_day_count_exactly():
    start, end = parse_time_range("30d")
    assert (end - start).days + 1 == 30
    start, end = parse_time_range("366d")
    assert (end - start).days + 1 == 366
    with pytest.raises(ValueError):
        parse_time_range("0d")
