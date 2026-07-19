"""Integration tests for resource authentication through the shared manager."""

from __future__ import annotations

import json

import pytest

import garmin_connect_mcp.session as session_module
from garmin_connect_mcp import server
from garmin_connect_mcp.client import GarminAuthenticationError
from garmin_connect_mcp.response_builder import ResponseBuilder


class FakeWrapper:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def safe_call(self, method_name, *args):
        self.calls.append((method_name, args))
        if self.error:
            raise self.error
        return {"totalSteps": 1234, "userProfileId": "CANARY_OWNER"}


class FakeManager:
    def __init__(self, client=None, error=None):
        self.client = client
        self.error = error
        self.calls = 0

    def get_read_client(self, preflight=None):
        self.calls += 1
        if preflight is not None:
            preflight()
        if self.error:
            raise self.error
        return self.client


@pytest.mark.asyncio
async def test_resource_uses_shared_session_manager(monkeypatch):
    wrapper = FakeWrapper()
    manager = FakeManager(client=wrapper)
    monkeypatch.setattr(session_module, "get_session_manager", lambda: manager)
    monkeypatch.setattr(server, "get_today_date_string", lambda: "2026-07-19")

    result = await server.health_today_resource()
    payload = json.loads(result)

    assert manager.calls == 1
    assert wrapper.calls == [("get_stats", ("2026-07-19",))]
    assert payload["data"]["health"] == {"totalSteps": 1234}


@pytest.mark.asyncio
async def test_resource_returns_shared_auth_error(monkeypatch, caplog):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")
    manager = FakeManager(error=GarminAuthenticationError("token missing"))
    monkeypatch.setattr(session_module, "get_session_manager", lambda: manager)

    result = await server.health_today_resource()
    payload = json.loads(result)

    assert manager.calls == 1
    assert payload["error"]["code"] == "AUTH_REQUIRED"
    assert payload["error"]["message"] == (
        "Garmin authentication is required. Run 'garmin-connect-mcp auth'."
    )
    actual_size = ResponseBuilder.serialized_envelope_size(result, "garmin://health/today")
    assert f"response_bytes={actual_size}" in caplog.messages[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("readiness", "expected_code"),
    [
        ([{"calendarDate": "2026-07-19"}] * 51, "ITEM_BUDGET_EXCEEDED"),
        (
            [{"calendarDate": "2026-07-19", "trainingReadinessScore": "x" * (1024 * 1024)}],
            "RESPONSE_BUDGET_EXCEEDED",
        ),
    ],
)
async def test_resource_budget_errors_log_the_actual_returned_envelope(
    monkeypatch,
    caplog,
    readiness,
    expected_code,
):
    caplog.set_level("INFO", logger="garmin_connect_mcp.query_budget")

    class ReadinessWrapper(FakeWrapper):
        def safe_call(self, method_name, *args):
            self.calls.append((method_name, args))
            return readiness

    monkeypatch.setattr(
        session_module,
        "get_session_manager",
        lambda: FakeManager(ReadinessWrapper()),
    )
    monkeypatch.setattr(server, "get_today_date_string", lambda: "2026-07-19")

    result = await server.training_readiness_resource()
    payload = json.loads(result)

    assert payload["error"]["code"] == expected_code
    actual_size = ResponseBuilder.serialized_envelope_size(
        result,
        "garmin://training/readiness",
    )
    assert f"response_bytes={actual_size}" in caplog.messages[-1]
    assert f"outcome={expected_code}" in caplog.messages[-1]


@pytest.mark.asyncio
async def test_resource_safe_call_failure_uses_public_error_boundary(monkeypatch):
    wrapper = FakeWrapper(error=RuntimeError("CANARY_SECRET upstream body"))
    manager = FakeManager(client=wrapper)
    monkeypatch.setattr(session_module, "get_session_manager", lambda: manager)

    result = await server.health_today_resource()
    payload = json.loads(result)

    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert payload["error"]["request_id"].startswith("err-")
    assert payload["metadata"]["response_schema"] == "2"
    assert "CANARY" not in result


@pytest.mark.asyncio
async def test_readiness_resource_uses_narrow_readiness_payload(monkeypatch):
    class ReadinessWrapper(FakeWrapper):
        def safe_call(self, method_name, *args):
            self.calls.append((method_name, args))
            return [
                {
                    "calendarDate": "2026-07-19",
                    "trainingReadinessScore": 82,
                    "recoveryTime": 120,
                    "totalSteps": 9000,
                    "weight": 75000,
                }
            ]

    wrapper = ReadinessWrapper()
    monkeypatch.setattr(session_module, "get_session_manager", lambda: FakeManager(wrapper))
    monkeypatch.setattr(server, "get_today_date_string", lambda: "2026-07-19")

    result = await server.training_readiness_resource()
    payload = json.loads(result)

    assert wrapper.calls == [("get_training_readiness", ("2026-07-19",))]
    assert payload["data"]["readiness"] == [
        {
            "calendarDate": "2026-07-19",
            "trainingReadinessScore": 82,
            "recoveryTime": 120,
        }
    ]
