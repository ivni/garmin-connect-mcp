"""Tests for shared-session MCP middleware."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from garmin_connect_mcp.client import GarminAuthenticationError
from garmin_connect_mcp.middleware import ConfigMiddleware


class FakeStateContext:
    def __init__(self):
        self.values: dict[str, Any] = {}

    async def set_state(self, key: str, value: Any, serializable: bool) -> None:
        assert serializable is False
        self.values[key] = value


class FakeManager:
    def __init__(self, client: Any = None, error: Exception | None = None):
        self.client = client
        self.error = error
        self.calls = 0

    def get_client(self) -> Any:
        self.calls += 1
        if self.error:
            raise self.error
        return self.client


@pytest.mark.asyncio
async def test_middleware_injects_manager_client():
    client = object()
    manager = FakeManager(client=client)
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    state = FakeStateContext()
    context = SimpleNamespace(fastmcp_context=state)

    async def call_next(received_context):
        assert received_context is context
        return "done"

    result = await middleware.on_call_tool(context, call_next)  # type: ignore[arg-type]

    assert result == "done"
    assert manager.calls == 1
    assert state.values["client"] is client


@pytest.mark.asyncio
async def test_middleware_exposes_actionable_auth_error():
    manager = FakeManager(error=GarminAuthenticationError("token missing"))
    middleware = ConfigMiddleware(manager)  # type: ignore[arg-type]
    context = SimpleNamespace(fastmcp_context=FakeStateContext())

    with pytest.raises(ToolError, match="token missing"):
        await middleware.on_call_tool(context, lambda _context: None)  # type: ignore[arg-type]
