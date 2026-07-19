"""Observable tool behavior for garminconnect 0.3.6 call mappings."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from garmin_connect_mcp import server, session
from garmin_connect_mcp.time_utils import get_today_date_string, parse_date_string
from garmin_connect_mcp.tools.activities import get_activity_social
from garmin_connect_mcp.tools.challenges import query_challenges
from garmin_connect_mcp.tools.devices import query_devices
from garmin_connect_mcp.tools.gear import query_gear
from garmin_connect_mcp.tools.training import get_performance_metrics, get_training_effect
from garmin_connect_mcp.tools.workouts import query_workouts


class RecordingClient:
    def __init__(self, results: dict[str, object] | None = None):
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.results = results or {}

    def safe_call(self, method_name: str, *args: object, **kwargs: object) -> object:
        self.calls.append((method_name, args, kwargs))
        return self.results.get(method_name, {})


class FakeContext:
    def __init__(self, client: RecordingClient):
        self.client = client

    async def get_state(self, key: str) -> RecordingClient:
        assert key == "client"
        return self.client


@pytest.mark.asyncio
async def test_workout_lookup_and_binary_download_use_supported_contracts():
    client = RecordingClient(
        {
            "get_workout_by_id": {"workoutId": 42},
            "download_workout": b"FIT\x00payload",
        }
    )
    context = FakeContext(client)

    lookup = json.loads(
        await query_workouts("get", 42, ctx=context)  # type: ignore[arg-type]
    )
    download = json.loads(
        await query_workouts("download", 42, ctx=context)  # type: ignore[arg-type]
    )

    assert lookup["data"]["workout"] == {"workoutId": 42}
    encoded = download["data"]["workout_file"]
    assert encoded["encoding"] == "base64"
    assert encoded["content_type"] == "application/vnd.garmin.fit"
    assert base64.b64decode(encoded["content_base64"]) == b"FIT\x00payload"
    assert [call[:2] for call in client.calls] == [
        ("get_workout_by_id", (42,)),
        ("download_workout", (42,)),
    ]


@pytest.mark.asyncio
async def test_activity_social_returns_a_stable_capability_error_without_a_client():
    payload = json.loads(await get_activity_social(42))

    assert payload["error"]["type"] == "capability_unavailable"
    assert "garminconnect==0.3.6" in payload["error"]["message"]


@pytest.mark.asyncio
async def test_challenge_endpoints_receive_their_required_page_arguments():
    client = RecordingClient({"get_earned_badges": []})

    await query_challenges(
        status="all",
        challenge_type="all",
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )

    assert ("get_available_badge_challenges", (0, 100), {}) in client.calls
    assert ("get_non_completed_badge_challenges", (0, 100), {}) in client.calls
    assert ("get_badge_challenges", (0, 100), {}) in client.calls
    assert ("get_adhoc_challenges", (0, 100), {}) in client.calls
    assert ("get_inprogress_virtual_challenges", (1, 100), {}) in client.calls


@pytest.mark.asyncio
async def test_device_solar_dates_and_alarm_signature_are_explicit():
    client = RecordingClient(
        {
            "get_devices": [],
            "get_device_solar_data": [],
            "get_device_alarms": [],
        }
    )

    await query_devices(
        device_id=123,
        include_last_used=False,
        include_primary=False,
        include_solar_data=True,
        solar_start_date="2026-07-18",
        solar_end_date="2026-07-19",
        include_alarms=True,
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )

    assert ("get_device_solar_data", (123, "2026-07-18", "2026-07-19"), {}) in client.calls
    assert ("get_device_alarms", (), {}) in client.calls


@pytest.mark.asyncio
async def test_gear_identifiers_are_mapped_to_the_required_methods():
    client = RecordingClient()

    await query_gear(
        "123",
        gear_uuid="gear-uuid",
        include_defaults=True,
        include_stats=True,
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )

    assert client.calls == [
        ("get_gear", ("123",), {}),
        ("get_gear_defaults", ("123",), {}),
        ("get_gear_stats", ("gear-uuid",), {}),
    ]


@pytest.mark.asyncio
async def test_fitness_age_and_training_effect_use_supported_methods():
    client = RecordingClient(
        {
            "get_activity": {
                "activityId": 42,
                "aerobicTrainingEffect": 3.2,
                "activityTrainingLoad": 81.0,
                "unrelated": "omitted",
            }
        }
    )
    context = FakeContext(client)

    await get_performance_metrics(
        date="2026-07-19",
        include_vo2_max=False,
        include_hill_score=False,
        include_endurance_score=False,
        include_hrv=False,
        include_fitness_age=True,
        ctx=context,  # type: ignore[arg-type]
    )
    effect = json.loads(
        await get_training_effect(activity_id=42, ctx=context)  # type: ignore[arg-type]
    )

    assert client.calls[0] == ("get_fitnessage_data", ("2026-07-19",), {})
    assert client.calls[1] == ("get_activity", (42,), {})
    assert effect["data"]["training_effect"] == {
        "aerobicTrainingEffect": 3.2,
        "activityTrainingLoad": 81.0,
    }


@pytest.mark.asyncio
async def test_resources_pass_concrete_dates_to_required_dependency_arguments(monkeypatch):
    client = RecordingClient()

    class Manager:
        def get_read_client(self) -> RecordingClient:
            return client

    monkeypatch.setattr(session, "get_session_manager", lambda: Manager())
    monkeypatch.setattr(server, "get_today_date_string", lambda: "2026-07-19")

    await server.athlete_profile_resource()
    await server.training_readiness_resource()
    await server.health_today_resource()

    assert ("get_user_summary", ("2026-07-19",), {}) in client.calls
    assert client.calls.count(("get_stats", ("2026-07-19",), {})) == 3


def test_relative_dates_follow_the_server_local_timezone():
    instant = datetime(2026, 7, 18, 22, 30, tzinfo=UTC)
    moscow = instant.astimezone(timezone(timedelta(hours=3)))
    new_york = instant.astimezone(timezone(timedelta(hours=-4)))

    assert get_today_date_string(now=moscow) == "2026-07-19"
    assert get_today_date_string(now=new_york) == "2026-07-18"
    assert parse_date_string("today", now=moscow).strftime("%Y-%m-%d") == "2026-07-19"
    assert parse_date_string("yesterday", now=moscow).strftime("%Y-%m-%d") == "2026-07-18"
