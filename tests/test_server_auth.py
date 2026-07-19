"""Integration tests for resource authentication through the shared manager."""

from __future__ import annotations

import json

import pytest

import garmin_connect_mcp.session as session_module
from garmin_connect_mcp import server
from garmin_connect_mcp.client import GarminAuthenticationError


class FakeWrapper:
    def __init__(self):
        self.calls = []

    def safe_call(self, method_name, *args):
        self.calls.append((method_name, args))
        return {"method": method_name}


class FakeManager:
    def __init__(self, client=None, error=None):
        self.client = client
        self.error = error
        self.calls = 0

    def get_read_client(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.client


@pytest.mark.asyncio
async def test_resource_uses_shared_session_manager(monkeypatch):
    wrapper = FakeWrapper()
    manager = FakeManager(client=wrapper)
    monkeypatch.setattr(session_module, "get_session_manager", lambda: manager)

    result = await server.health_today_resource()
    payload = json.loads(result)

    assert manager.calls == 1
    assert wrapper.calls == [("get_stats", ("today",))]
    assert payload["data"]["health"] == {"method": "get_stats"}


@pytest.mark.asyncio
async def test_resource_returns_shared_auth_error(monkeypatch):
    manager = FakeManager(error=GarminAuthenticationError("token missing"))
    monkeypatch.setattr(session_module, "get_session_manager", lambda: manager)

    result = await server.health_today_resource()
    payload = json.loads(result)

    assert manager.calls == 1
    assert payload["error"]["message"] == "token missing"
