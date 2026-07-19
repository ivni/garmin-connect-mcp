"""Tests for bounded mutation tools using doubles only."""

from __future__ import annotations

import json

import pytest

from garmin_connect_mcp.response_builder import ResponseBuilder
from garmin_connect_mcp.tools.data_management import (
    log_blood_pressure,
    log_body_composition,
    log_hydration,
)
from garmin_connect_mcp.tools.weight import add_weight_entry, delete_weight_entries
from garmin_connect_mcp.tools.workouts import upload_workout


class FakeMutationClient:
    def __init__(self):
        self.calls = []

    def mutate(self, method_name, *args, idempotency_key, **kwargs):
        self.calls.append((method_name, args, kwargs, idempotency_key))
        return {"accepted": True}


class FakeContext:
    def __init__(self, client: FakeMutationClient):
        self.client = client
        self.calls = 0

    async def get_state(self, key: str):
        assert key == "client"
        self.calls += 1
        return self.client


@pytest.mark.asyncio
async def test_weight_dry_run_does_not_need_a_client():
    payload = json.loads(await add_weight_entry(75.5, "2026-07-19"))

    assert payload["data"]["preview"] == {"weight": 75.5, "date": "2026-07-19"}
    assert payload["metadata"]["dry_run"] is True


@pytest.mark.asyncio
async def test_weight_input_is_bounded_before_client_lookup():
    client = FakeMutationClient()
    context = FakeContext(client)

    payload = json.loads(
        await add_weight_entry(
            0,
            idempotency_key="request-0001",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )

    assert payload["error"]["type"] == "invalid_parameters"
    assert context.calls == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_weight_execution_uses_mutation_facade_and_idempotency_key():
    client = FakeMutationClient()
    context = FakeContext(client)

    payload = json.loads(
        await add_weight_entry(
            75,
            "2026-07-19",
            idempotency_key="request-0001",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )

    assert payload["metadata"]["dry_run"] is False
    assert client.calls == [
        (
            "add_weigh_in",
            (75, "kg", "2026-07-19T12:00:00"),
            {},
            "request-0001",
        )
    ]


@pytest.mark.asyncio
async def test_delete_requires_confirmation_from_preview():
    preview = json.loads(await delete_weight_entries("2026-07-19"))
    required = preview["data"]["preview"]["required_confirmation"]
    client = FakeMutationClient()
    context = FakeContext(client)

    denied = json.loads(
        await delete_weight_entries(
            "2026-07-19",
            confirmation="yes",
            idempotency_key="request-0002",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )
    accepted = json.loads(
        await delete_weight_entries(
            "2026-07-19",
            confirmation=required,
            idempotency_key="request-0002",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )

    assert denied["error"]["type"] == "invalid_parameters"
    assert accepted["metadata"]["capability"] == "weight.delete"
    assert client.calls == [("delete_weigh_ins", ("2026-07-19", True), {}, "request-0002")]


@pytest.mark.asyncio
async def test_delete_rejects_invalid_date_before_client_lookup():
    client = FakeMutationClient()
    context = FakeContext(client)

    payload = json.loads(
        await delete_weight_entries(
            "not-a-date",
            confirmation="DELETE WEIGH-INS ON not-a-date",
            idempotency_key="request-0002",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )

    assert payload["error"]["type"] == "invalid_parameters"
    assert "Invalid date format" in payload["error"]["message"]
    assert context.calls == 0
    assert client.calls == []


@pytest.mark.asyncio
async def test_workout_preview_validates_size_and_structure_locally():
    valid = json.loads(await upload_workout('{"workoutName":"Tempo"}'))
    invalid = json.loads(await upload_workout('"not an object"'))

    assert valid["data"]["preview"]["size_bytes"] > 0
    assert len(valid["data"]["preview"]["sha256"]) == 64
    assert invalid["error"]["type"] == "invalid_parameters"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "data"),
    [
        (log_body_composition, '{"weight":501}'),
        (log_blood_pressure, '{"systolic":120,"diastolic":120,"pulse":60}'),
        (log_hydration, '{"volume_ml":5001}'),
    ],
)
async def test_health_inputs_are_bounded_without_client(tool, data):
    payload = json.loads(await tool(data))

    assert payload["error"]["type"] == "invalid_parameters"


@pytest.mark.asyncio
async def test_each_health_tool_uses_its_own_capability_method():
    client = FakeMutationClient()
    context = FakeContext(client)

    body = json.loads(
        await log_body_composition(
            '{"weight":75,"body_fat":15}',
            "2026-07-19",
            idempotency_key="request-body",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )
    blood = json.loads(
        await log_blood_pressure(
            '{"systolic":120,"diastolic":80,"pulse":60}',
            "2026-07-19",
            idempotency_key="request-blood",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )
    hydration = json.loads(
        await log_hydration(
            '{"volume_ml":500}',
            "2026-07-19",
            idempotency_key="request-water",
            dry_run=False,
            ctx=context,  # type: ignore[arg-type]
        )
    )

    assert body["metadata"]["capability"] == "health.body_composition"
    assert blood["metadata"]["capability"] == "health.blood_pressure"
    assert hydration["metadata"]["capability"] == "health.hydration"
    assert [call[0] for call in client.calls] == [
        "add_body_composition",
        "set_blood_pressure",
        "add_hydration_data",
    ]
    assert client.calls[0][1] == ()
    assert client.calls[0][2] == {
        "timestamp": "2026-07-19T12:00:00",
        "weight": 75.0,
        "percent_fat": 15.0,
        "percent_hydration": None,
    }
    assert client.calls[1][1] == (120, 80, 60, "2026-07-19T12:00:00")
    assert client.calls[1][2] == {}
    assert client.calls[2][1] == (500.0,)
    assert client.calls[2][2] == {
        "timestamp": "2026-07-19T12:00:00",
        "cdate": "2026-07-19",
    }


def test_write_projection_replaces_unknown_scalar_result_with_local_acknowledgement():
    response = ResponseBuilder.build_response(
        {"result": "CANARY_SECRET upstream acknowledgement"},
        surface="add_weight_entry",
    )
    payload = json.loads(response)

    assert payload["data"]["result"] == {"acknowledged": True}
    assert "CANARY" not in response
