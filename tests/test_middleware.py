"""Tests for least-privilege shared-session MCP middleware."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import CallToolRequestParams

from garmin_connect_mcp.client import GarminAuthenticationError
from garmin_connect_mcp.middleware import ConfigMiddleware
from garmin_connect_mcp.write_policy import WritePolicy


class FakeStateContext:
    def __init__(self):
        self.values: dict[str, Any] = {}

    async def set_state(self, key: str, value: Any, serializable: bool) -> None:
        assert serializable is False
        self.values[key] = value


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

    def get_read_client(self) -> Any:
        self.read_calls += 1
        if self.error:
            raise self.error
        return self.read_client

    def get_mutation_client(self, operation) -> Any:
        self.mutation_calls.append(operation)
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
    assert manager.read_calls == 1
    assert manager.mutation_calls == []
    assert context.fastmcp_context.values["client"] is client


@pytest.mark.asyncio
async def test_middleware_exposes_actionable_auth_error():
    manager = FakeManager(error=GarminAuthenticationError("CANARY_SECRET token missing"))
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = tool_context("query_weight_data")

    with pytest.raises(ToolError) as caught:
        await middleware.on_call_tool(context, lambda _context: None)  # type: ignore[arg-type]

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in str(caught.value)


@pytest.mark.asyncio
async def test_disabled_write_fails_before_any_client_is_requested():
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
async def test_enabled_write_receives_only_its_mutation_client():
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
    assert len(manager.mutation_calls) == 1
    operation = manager.mutation_calls[0]
    assert operation.capability == "weight.write"
    assert operation.method_name == "add_weigh_in"
    assert context.fastmcp_context.values["client"] is client


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
    async def query_weight_data() -> str:
        return "unreachable"

    with pytest.raises(ToolError) as caught:
        await app.call_tool("query_weight_data", {})

    payload = json.loads(str(caught.value))
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in str(caught.value)
