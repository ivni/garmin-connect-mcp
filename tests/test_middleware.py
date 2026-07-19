"""Tests for least-privilege shared-session MCP middleware."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from mcp.types import CallToolRequestParams

from garmin_connect_mcp.client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminClientWrapper,
    GarminMutationClient,
    GarminMutationOutcomeUnknownError,
    GarminRateLimitError,
    MutationOperation,
    MutationRegistry,
)
from garmin_connect_mcp.middleware import ConfigMiddleware
from garmin_connect_mcp.query_budget import (
    BudgetedGarminMutationClient,
    BudgetedGarminReadClient,
    QueryBudgetPolicy,
    RequestBudget,
    policy_for_surface,
)
from garmin_connect_mcp.response_builder import ResponseBuilder
from garmin_connect_mcp.tools.health_wellness import query_sleep_data
from garmin_connect_mcp.write_policy import WritePolicy


class FakeStateContext:
    def __init__(self):
        self.values: dict[str, Any] = {}

    async def set_state(self, key: str, value: Any, serializable: bool) -> None:
        assert serializable is False
        self.values[key] = value

    async def get_state(self, key: str) -> Any:
        return self.values[key]


class FakeManager:
    def __init__(
        self,
        read_client: Any = None,
        mutation_client: Any = None,
        error: Exception | None = None,
    ):
        self.read_client = read_client
        self.mutation_client = mutation_client
        self.error = error
        self.read_calls = 0
        self.mutation_calls = []

    def get_read_client(self, preflight=None) -> Any:
        self.read_calls += 1
        if preflight is not None:
            preflight()
        if self.error:
            raise self.error
        return self.read_client

    def get_mutation_client(self, operation, preflight=None) -> Any:
        self.mutation_calls.append(operation)
        if preflight is not None:
            preflight()
        if self.error:
            raise self.error
        return self.mutation_client


def tool_context(name: str, arguments: dict[str, Any] | None = None):
    return SimpleNamespace(
        message=CallToolRequestParams(name=name, arguments=arguments),
        fastmcp_context=FakeStateContext(),
    )


@pytest.mark.asyncio
async def test_middleware_injects_read_only_manager_client():
    client = object()
    manager = FakeManager(read_client=client)
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    async def call_next(received_context):
        assert received_context is context
        return "done"

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert result == "done"
    assert manager.read_calls == 0
    assert manager.mutation_calls == []
    injected = context.fastmcp_context.values["client"]
    assert isinstance(injected, BudgetedGarminReadClient)
    budget = context.fastmcp_context.values["request_budget"]
    assert isinstance(budget, RequestBudget)
    assert injected.budget is budget
    assert budget.surface == "query_weight_data"


@pytest.mark.asyncio
async def test_middleware_replaces_an_oversized_tool_payload_with_a_bounded_error(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    manager = FakeManager(read_client=object())
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    async def call_next(_context):
        return "x" * ((1024 * 1024) + 1)

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert len(str(caught.value).encode("utf-8")) < 1024 * 1024
    actual_size = ResponseBuilder.serialized_tool_error_size(str(caught.value))
    assert f"response_bytes={actual_size}" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_oversized_tool_error_is_replaced_inside_the_same_exception_branch(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    middleware = ConfigMiddleware(FakeManager(read_client=object()))  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    async def call_next(_context):
        raise ToolError("x" * ((1024 * 1024) + 1))

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    actual_size = ResponseBuilder.serialized_tool_error_size(str(caught.value))
    assert f"response_bytes={actual_size}" in caplog.messages[-1]
    assert "outcome=RESPONSE_BUDGET_EXCEEDED" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_returned_validation_error_sets_budget_outcome(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    middleware = ConfigMiddleware(FakeManager(read_client=object()))  # type: ignore[arg-type]
    context = tool_context("query_workouts", {"action": "invalid"})

    async def call_next(_context):
        return ResponseBuilder.build_error_response(
            "invalid action",
            "validation_error",
        )

    await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert "outcome=VALIDATION_ERROR" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_fastmcp_schema_validation_is_a_stable_validation_error(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    app = FastMCP("schema-validation-budget")
    app.add_middleware(ConfigMiddleware(FakeManager()))  # type: ignore[arg-type]

    @app.tool
    async def query_workouts(limit: int = 10) -> str:
        return str(limit)

    async with Client(app) as client:
        with pytest.raises(ToolError) as caught:
            await client.call_tool("query_workouts", {"limit": ["invalid"]})

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert "outcome=VALIDATION_ERROR" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_final_tool_serialization_is_inside_the_request_deadline(monkeypatch):
    release = threading.Event()
    policy = QueryBudgetPolicy(
        max_range_days=None,
        default_page_items=1,
        max_page_items=1,
        max_api_calls=1,
        max_response_bytes=64 * 1024,
        timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.policy_for_surface",
        lambda _surface: policy,
    )
    original = ResponseBuilder.serialized_result_size

    def blocking_size(result, surface):
        release.wait(timeout=2)
        return original(result, surface)

    monkeypatch.setattr(ResponseBuilder, "serialized_result_size", blocking_size)
    middleware = ConfigMiddleware(FakeManager(read_client=object()))  # type: ignore[arg-type]
    context = tool_context("query_weight_data")
    try:
        with pytest.raises(ToolError) as caught:
            await middleware.on_call_tool(
                context,
                lambda _context: asyncio.sleep(0, result="done"),
            )  # type: ignore[arg-type]
    finally:
        release.set()

    assert json.loads(str(caught.value))["error"]["code"] == "REQUEST_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_finalization_failure_after_mutation_dispatch_preserves_unknown_outcome(
    monkeypatch,
):
    from garmin_connect_mcp.query_budget import RequestDeadlineExceededError

    async def deadline(_invoke, budget):
        budget.expire()
        raise RequestDeadlineExceededError

    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.run_bounded_finalization",
        deadline,
    )
    middleware = ConfigMiddleware(
        FakeManager(mutation_client=object()),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        budget = await received_context.fastmcp_context.get_state("request_budget")
        budget.mark_mutation_dispatched()
        return "delivery-pending"

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert "Reconcile" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_durable_unknown_replay_survives_a_finalization_deadline(monkeypatch):
    from garmin_connect_mcp.query_budget import RequestDeadlineExceededError

    class Dependency:
        def upload_workout(self, _workout: object) -> object:
            raise RuntimeError("ambiguous transport failure")

    production = GarminMutationClient(
        GarminClientWrapper(Dependency()),  # type: ignore[arg-type]
        MutationOperation("workouts.upload", "upload_workout", "check the workout"),
        MutationRegistry(),
    )
    first_budget = RequestBudget("upload_workout", policy_for_surface("upload_workout"))
    with pytest.raises(GarminMutationOutcomeUnknownError):
        await BudgetedGarminMutationClient(production, first_budget).mutate(
            "upload_workout",
            {},
            idempotency_key="durable-unknown-key",
        )
    assert first_budget.mutation_outcome == "uncertain"

    async def deadline(_invoke, budget):
        budget.expire()
        raise RequestDeadlineExceededError

    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.run_bounded_finalization",
        deadline,
    )
    middleware = ConfigMiddleware(
        FakeManager(mutation_client=production),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        try:
            await client.mutate(
                "upload_workout",
                {},
                idempotency_key="durable-unknown-key",
            )
        except GarminMutationOutcomeUnknownError as exc:
            return ResponseBuilder.build_exception_response(exc)
        raise AssertionError("Unknown durable outcome must not be replayed")

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    budget = context.fastmcp_context.values["request_budget"]
    assert budget.mutation_dispatched is False
    assert budget.mutation_outcome == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "definite_error",
    [GarminAuthenticationError(), GarminRateLimitError()],
)
async def test_definite_mutation_rejection_is_not_changed_to_unknown_by_finalization(
    monkeypatch,
    definite_error,
):
    from garmin_connect_mcp.query_budget import RequestDeadlineExceededError

    class Dependency:
        def upload_workout(self, _workout: object) -> object:
            raise definite_error

    production = GarminMutationClient(
        GarminClientWrapper(Dependency()),  # type: ignore[arg-type]
        MutationOperation("workouts.upload", "upload_workout", "check the workout"),
        MutationRegistry(),
    )

    async def deadline(_invoke, budget):
        budget.expire()
        raise RequestDeadlineExceededError

    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.run_bounded_finalization",
        deadline,
    )
    middleware = ConfigMiddleware(
        FakeManager(mutation_client=production),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        try:
            await client.mutate(
                "upload_workout",
                {},
                idempotency_key="definite-rejection-key",
            )
        except GarminAPIError as exc:
            return ResponseBuilder.build_exception_response(exc)
        raise AssertionError("The dependency rejection must propagate")

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "REQUEST_DEADLINE_EXCEEDED"
    budget = context.fastmcp_context.values["request_budget"]
    assert budget.mutation_dispatched is True
    assert budget.mutation_outcome == "definitely_failed"


@pytest.mark.asyncio
async def test_invalid_idempotency_key_fails_before_session_resolution():
    middleware = ConfigMiddleware(
        manager := FakeManager(mutation_client=object()),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        try:
            await client.mutate(
                "upload_workout",
                {},
                idempotency_key="short",
            )
        except GarminAPIError as exc:
            return ResponseBuilder.build_exception_response(exc)
        raise AssertionError("Malformed key must be rejected")

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert json.loads(result)["error"]["code"] == "VALIDATION_ERROR"
    assert manager.mutation_calls == []
    budget = context.fastmcp_context.values["request_budget"]
    assert budget.calls_used == 0
    assert budget.mutation_outcome == "definitely_failed"


@pytest.mark.asyncio
async def test_confirmed_mutation_gets_bounded_success_not_a_retry_facing_error():
    class MutationClient:
        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            return {"success": True}

    manager = FakeManager(mutation_client=MutationClient())
    middleware = ConfigMiddleware(
        manager,  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="confirmed-key",
        )
        return "x" * (80 * 1024)

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert isinstance(result, ToolResult)
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert "error" not in payload
    assert payload["metadata"]["mutation_confirmed"] is True
    assert payload["metadata"]["result_omitted"] == "response_budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("post_write_failure", ["returned_error", "raised_error"])
async def test_confirmed_mutation_hides_every_post_write_retry_signal(
    post_write_failure,
    caplog,
):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")

    class MutationClient:
        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            return {"success": True}

    middleware = ConfigMiddleware(
        FakeManager(mutation_client=MutationClient()),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="post-write-key",
        )
        if post_write_failure == "raised_error":
            raise RuntimeError("projection failed")
        return ResponseBuilder.build_error_response("projection failed")

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert isinstance(result, ToolResult)
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert "error" not in payload
    assert payload["metadata"]["mutation_confirmed"] is True
    assert payload["metadata"]["result_omitted"] == "post_confirmation_failure"
    assert "outcome=confirmed" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_real_fastmcp_serializes_post_confirmation_failure_as_success():
    class MutationClient:
        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            return {"success": True}

    app = FastMCP("confirmed-mutation-protocol")
    app.add_middleware(
        ConfigMiddleware(
            FakeManager(mutation_client=MutationClient()),  # type: ignore[arg-type]
            WritePolicy(frozenset({"workouts.upload"})),
        )
    )

    @app.tool
    async def upload_workout(dry_run: bool = False, ctx: Context | None = None) -> str:
        assert dry_run is False
        assert ctx is not None
        client = await ctx.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="real-protocol-key",
        )
        raise RuntimeError("projection failed")

    async with Client(app) as client:
        result = await client.call_tool("upload_workout", {"dry_run": False})

    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert result.is_error is False
    assert payload["metadata"]["mutation_confirmed"] is True
    assert payload["metadata"]["result_omitted"] == "post_confirmation_failure"


@pytest.mark.asyncio
async def test_started_mutation_deadline_returns_unknown_outcome_not_safe_retry(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingMutationClient:
        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            started.set()
            release.wait(timeout=2)
            return {"success": True}

    policy = QueryBudgetPolicy(
        max_range_days=None,
        default_page_items=1,
        max_page_items=1,
        max_api_calls=1,
        max_response_bytes=64 * 1024,
        timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.policy_for_surface",
        lambda _surface: policy,
    )
    manager = FakeManager(mutation_client=BlockingMutationClient())
    middleware = ConfigMiddleware(
        manager,  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="deadline-key",
        )
        return "unreachable"

    try:
        with pytest.raises(ToolError) as caught:
            await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    finally:
        release.set()

    assert started.is_set()
    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert "Reconcile" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_late_deadline_after_confirmed_mutation_returns_compact_success(monkeypatch):
    class ImmediateMutationClient:
        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            return {"success": True}

    policy = QueryBudgetPolicy(
        max_range_days=None,
        default_page_items=1,
        max_page_items=1,
        max_api_calls=1,
        max_response_bytes=64 * 1024,
        timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.policy_for_surface",
        lambda _surface: policy,
    )
    middleware = ConfigMiddleware(
        FakeManager(mutation_client=ImmediateMutationClient()),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="confirmed-before-deadline",
        )
        await asyncio.sleep(0.1)
        return "unreachable"

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    assert isinstance(result, ToolResult)
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]

    assert payload["metadata"]["mutation_confirmed"] is True
    assert payload["metadata"]["result_omitted"] == "deadline_delivery"


@pytest.mark.asyncio
async def test_deadline_includes_waiting_for_the_session_client(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class BlockingManager(FakeManager):
        def get_read_client(self, preflight=None) -> Any:
            self.read_calls += 1
            started.set()
            release.wait(timeout=2)
            if preflight is not None:
                preflight()
            return self.read_client

    class RawClient:
        def safe_call(self, _method_name: str) -> object:
            return {}

    policy = QueryBudgetPolicy(
        max_range_days=1,
        default_page_items=1,
        max_page_items=1,
        max_api_calls=1,
        max_response_bytes=4096,
        timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.policy_for_surface",
        lambda _surface: policy,
    )
    manager = BlockingManager(read_client=RawClient())
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.call("get_daily_weigh_ins")
        return "unreachable"

    try:
        with pytest.raises(ToolError) as caught:
            await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    finally:
        release.set()

    assert started.is_set()
    assert json.loads(str(caught.value))["error"]["code"] == "REQUEST_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_mutation_session_wait_expires_before_remote_dispatch(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class MutationClient:
        def __init__(self):
            self.calls = 0

        def mutate(self, _method_name: str, *_args, **_kwargs) -> object:
            self.calls += 1
            return {"success": True}

    mutation_client = MutationClient()

    class BlockingManager(FakeManager):
        def get_mutation_client(self, operation, preflight=None) -> Any:
            self.mutation_calls.append(operation)
            started.set()
            release.wait(timeout=2)
            if preflight is not None:
                preflight()
            return mutation_client

    policy = QueryBudgetPolicy(
        max_range_days=None,
        default_page_items=1,
        max_page_items=1,
        max_api_calls=1,
        max_response_bytes=64 * 1024,
        timeout_seconds=0.05,
    )
    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.policy_for_surface",
        lambda _surface: policy,
    )
    middleware = ConfigMiddleware(
        BlockingManager(mutation_client=mutation_client),  # type: ignore[arg-type]
        WritePolicy(frozenset({"workouts.upload"})),
    )
    context = tool_context("upload_workout", {"dry_run": False})

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.mutate(
            "upload_workout",
            {},
            idempotency_key="session-wait-key",
        )
        return "unreachable"

    try:
        with pytest.raises(ToolError) as caught:
            await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]
    finally:
        release.set()

    assert started.is_set()
    assert json.loads(str(caught.value))["error"]["code"] == "REQUEST_DEADLINE_EXCEEDED"
    await asyncio.sleep(0.05)
    assert mutation_client.calls == 0


@pytest.mark.asyncio
async def test_middleware_exposes_actionable_auth_error(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    manager = FakeManager(error=GarminAuthenticationError("CANARY_SECRET token missing"))
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    async def call_next(received_context):
        client = await received_context.fastmcp_context.get_state("client")
        await client.call("get_daily_weigh_ins", "2026-07-19")

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in str(caught.value)
    assert "outcome=AUTH_REQUIRED" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_disabled_write_fails_before_any_client_is_requested(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    manager = FakeManager()
    middleware = ConfigMiddleware(manager, WritePolicy())  # type: ignore[arg-type]
    context = tool_context("add_weight_entry", {"dry_run": False})

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, lambda _context: None)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert "disabled by server policy" in payload["error"]["message"]
    assert manager.read_calls == 0
    assert manager.mutation_calls == []
    assert "outcome=CAPABILITY_UNAVAILABLE" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_enabling_one_capability_does_not_enable_another():
    manager = FakeManager()
    middleware = ConfigMiddleware(
        manager,  # type: ignore[arg-type]
        WritePolicy(frozenset({"weight.write"})),
    )
    context = tool_context("log_hydration", {"dry_run": False})

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, lambda _context: None)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert manager.read_calls == 0
    assert manager.mutation_calls == []


@pytest.mark.asyncio
async def test_dry_run_uses_no_authenticated_client():
    manager = FakeManager()
    middleware = ConfigMiddleware(manager, WritePolicy())  # type: ignore[arg-type]
    context = tool_context("delete_weight_entries", {"dry_run": True})

    async def call_next(_context):
        return "preview"

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert result == "preview"
    assert manager.read_calls == 0
    assert manager.mutation_calls == []
    assert context.fastmcp_context.values == {}


@pytest.mark.asyncio
async def test_enabled_write_receives_a_lazy_budgeted_mutation_client():
    client = object()
    manager = FakeManager(mutation_client=client)
    middleware = ConfigMiddleware(
        manager,  # type: ignore[arg-type]
        WritePolicy(frozenset({"weight.write"})),
    )
    context = tool_context("add_weight_entry", {"dry_run": False})

    async def call_next(_context):
        return "done"

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert result == "done"
    assert manager.read_calls == 0
    assert manager.mutation_calls == []
    injected = context.fastmcp_context.values["client"]
    assert isinstance(injected, BudgetedGarminMutationClient)
    budget = context.fastmcp_context.values["request_budget"]
    assert isinstance(budget, RequestBudget)
    assert injected.budget is budget
    assert budget.surface == "add_weight_entry"


@pytest.mark.asyncio
async def test_real_fastmcp_call_uses_direct_call_tool_params():
    manager = FakeManager()
    app = FastMCP("middleware-contract")
    app.add_middleware(
        ConfigMiddleware(manager, WritePolicy())  # type: ignore[arg-type]
    )

    @app.tool
    async def add_weight_entry(dry_run: bool = True) -> str:
        return "preview" if dry_run else "write"

    result = await app.call_tool("add_weight_entry", {"dry_run": True})

    assert result.content[0].text == "preview"  # type: ignore[union-attr]
    with pytest.raises(ToolError) as caught:
        await app.call_tool("add_weight_entry", {"dry_run": False})
    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert payload["metadata"]["response_schema"] == "2"
    assert manager.read_calls == 0
    assert manager.mutation_calls == []


@pytest.mark.asyncio
async def test_real_fastmcp_call_sanitizes_unexpected_middleware_failure():
    manager = FakeManager(error=RuntimeError("CANARY_SECRET middleware failure"))
    app = FastMCP("middleware-error-contract")
    app.add_middleware(
        ConfigMiddleware(manager, WritePolicy())  # type: ignore[arg-type]
    )

    @app.tool
    async def query_weight_data(ctx: Context) -> str:
        client = await ctx.get_state("client")
        await client.call("get_daily_weigh_ins", "2026-07-19")
        return "unreachable"

    with pytest.raises(ToolError) as caught:
        await app.call_tool("query_weight_data", {})

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in str(caught.value)


@pytest.mark.asyncio
async def test_real_fastmcp_tool_result_is_measured_at_the_final_boundary():
    manager = FakeManager()
    app = FastMCP("middleware-tool-result-budget")
    app.add_middleware(ConfigMiddleware(manager))  # type: ignore[arg-type]

    @app.tool
    async def upload_workout(dry_run: bool = True) -> str:
        assert dry_run is True
        return "x" * (64 * 1024)

    with pytest.raises(ToolError) as caught:
        await app.call_tool("upload_workout", {"dry_run": True})

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert len(str(caught.value).encode("utf-8")) < 64 * 1024


@pytest.mark.asyncio
async def test_real_fastmcp_bounded_page_fits_the_same_final_tool_envelope():
    manager = FakeManager()
    app = FastMCP("middleware-bounded-tool-envelope")
    app.add_middleware(ConfigMiddleware(manager))  # type: ignore[arg-type]

    @app.tool
    async def query_workouts() -> str:
        return ResponseBuilder.build_bounded_collection_response(
            items=[
                {"workoutName": "a", "description": "x" * 200_000},
                {"workoutName": "b", "description": "y" * 200_000},
                {"workoutName": "c", "description": "z" * 200_000},
            ],
            data_factory=lambda values: {"workouts": values},
            metadata_factory=lambda _count: {},
            pagination={"cursor": None, "has_more": False, "limit": 3, "returned": 3},
            cursor_factory=lambda count: f"cursor-{count}",
            surface="query_workouts",
        )

    async with Client(app) as client:
        result = await client.call_tool("query_workouts", {})
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]

    assert payload["pagination"]["partial"] is True
    assert payload["pagination"]["truncation_reason"] == "response_bytes"
    assert payload["pagination"]["returned"] == 2
    assert (
        ResponseBuilder.serialized_envelope_size(
            result.content[0].text,  # type: ignore[union-attr]
            "query_workouts",
        )
        <= policy_for_surface("query_workouts").max_response_bytes
    )


@pytest.mark.asyncio
async def test_resource_final_guard_bounds_a_builder_bypassing_payload(caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    app = FastMCP("middleware-resource-envelope")
    app.add_middleware(ConfigMiddleware(FakeManager()))  # type: ignore[arg-type]

    @app.resource("garmin://health/today")
    async def oversized_health_resource() -> str:
        return "x" * ((1024 * 1024) + 1)

    result = await app.read_resource("garmin://health/today")
    payload = json.loads(result.contents[0].content)

    assert payload["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert (
        ResponseBuilder.serialized_result_size(result, "garmin://health/today")
        <= policy_for_surface("garmin://health/today").max_response_bytes
    )
    assert "outcome=RESPONSE_BUDGET_EXCEEDED" in caplog.messages[-1]
    assert "truncation=response_bytes" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_resource_final_serialization_deadline_returns_a_bounded_error(monkeypatch):
    from garmin_connect_mcp.query_budget import RequestDeadlineExceededError

    async def deadline(_invoke, budget):
        budget.expire()
        raise RequestDeadlineExceededError

    monkeypatch.setattr(
        "garmin_connect_mcp.middleware.run_bounded_finalization",
        deadline,
    )
    app = FastMCP("middleware-resource-deadline")
    app.add_middleware(ConfigMiddleware(FakeManager()))  # type: ignore[arg-type]

    @app.resource("garmin://health/today")
    async def health_resource() -> str:
        return "ok"

    result = await app.read_resource("garmin://health/today")
    payload = json.loads(result.contents[0].content)

    assert payload["error"]["code"] == "REQUEST_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_rejected_range_does_not_resolve_the_lazy_session_client():
    manager = FakeManager()
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context(
        "query_sleep_data",
        {"start_date": "2024-01-01", "end_date": "2024-02-01"},
    )

    async def call_next(received_context):
        return await query_sleep_data(
            start_date="2024-01-01",
            end_date="2024-02-01",
            ctx=received_context.fastmcp_context,
        )

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert json.loads(result)["error"]["code"] == "RANGE_TOO_LARGE"
    assert manager.read_calls == 0
