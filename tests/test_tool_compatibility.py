"""Observable tool behavior for garminconnect 0.3.6 call mappings."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from garmin_connect_mcp import server, session
from garmin_connect_mcp.pagination import (
    MAX_CONTINUATION_POSITION,
    encode_continuation_cursor,
)
from garmin_connect_mcp.query_budget import (
    RequestBudget,
    policy_for_surface,
    reset_current_request_budget,
    set_current_request_budget,
)
from garmin_connect_mcp.time_utils import get_today_date_string, parse_date_string
from garmin_connect_mcp.tools.activities import get_activity_social
from garmin_connect_mcp.tools.analysis import find_similar_activities
from garmin_connect_mcp.tools.challenges import (
    _normalize_challenge_page,
    query_challenges,
    query_goals_and_records,
)
from garmin_connect_mcp.tools.devices import query_devices
from garmin_connect_mcp.tools.gear import query_gear
from garmin_connect_mcp.tools.training import (
    analyze_training_period,
    get_performance_metrics,
    get_training_effect,
)
from garmin_connect_mcp.tools.user_profile import get_user_profile
from garmin_connect_mcp.tools.workouts import query_workouts


class RecordingClient:
    def __init__(self, results: dict[str, object] | None = None):
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.results = results or {}

    def safe_call(self, method_name: str, *args: object, **kwargs: object) -> object:
        self.calls.append((method_name, args, kwargs))
        return self.results.get(method_name, {})

    async def call(self, method_name: str, *args: object, **kwargs: object) -> object:
        return self.safe_call(method_name, *args, **kwargs)


class FakeContext:
    def __init__(self, client: RecordingClient):
        self.client = client

    async def get_state(self, key: str) -> RecordingClient:
        assert key == "client"
        return self.client


@pytest.mark.asyncio
async def test_unpaged_profile_gear_and_record_collections_fail_closed_at_item_ceiling():
    scenarios = [
        (
            "query_gear",
            RecordingClient({"get_gear": [{"displayName": "item"}] * 51}),
            lambda context: query_gear(
                "123",
                include_defaults=False,
                ctx=context,  # type: ignore[arg-type]
            ),
        ),
        (
            "get_user_profile",
            RecordingClient(
                {
                    "get_full_name": "Athlete",
                    "get_user_profile": {},
                    "get_personal_record": [{"typeId": 1}] * 51,
                }
            ),
            lambda context: get_user_profile(
                include_stats=False,
                include_prs=True,
                include_devices=False,
                ctx=context,  # type: ignore[arg-type]
            ),
        ),
        (
            "query_goals_and_records",
            RecordingClient({"get_personal_record": [{"typeId": 1}] * 51}),
            lambda context: query_goals_and_records(
                include_goals=False,
                include_prs=True,
                include_race_predictions=False,
                ctx=context,  # type: ignore[arg-type]
            ),
        ),
    ]

    for surface, client, invoke in scenarios:
        budget = RequestBudget(surface, policy_for_surface(surface))
        token = set_current_request_budget(budget)
        try:
            payload = json.loads(await invoke(FakeContext(client)))
        finally:
            reset_current_request_budget(token)
        assert payload["error"]["code"] == "ITEM_BUDGET_EXCEEDED"
        assert budget.items_used == 0


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
async def test_workout_list_is_bounded_and_cursor_paged():
    workouts = [{"workoutId": item} for item in range(20)]
    client = RecordingClient({"get_workouts": workouts})

    response = json.loads(
        await query_workouts("list", ctx=FakeContext(client))  # type: ignore[arg-type]
    )

    assert client.calls == [("get_workouts", (0, 21), {})]
    assert response["pagination"]["returned"] == 20
    assert response["pagination"]["has_more"] is False
    assert response["pagination"]["cursor"] is None


@pytest.mark.asyncio
async def test_workout_list_uses_one_item_lookahead_for_continuation():
    workouts = [{"workoutId": item} for item in range(21)]
    client = RecordingClient({"get_workouts": workouts})

    response = json.loads(
        await query_workouts("list", ctx=FakeContext(client))  # type: ignore[arg-type]
    )

    assert len(response["data"]["workouts"]) == 20
    assert response["pagination"]["returned"] == 20
    assert response["pagination"]["has_more"] is True
    assert response["pagination"]["cursor"]


@pytest.mark.asyncio
async def test_oversized_fit_is_rejected_before_base64_expansion(monkeypatch):
    client = RecordingClient({"download_workout": b"x" * (500 * 1024)})
    budget = RequestBudget("query_workouts", policy_for_surface("query_workouts"))
    token = set_current_request_budget(budget)
    encoded = False

    def unexpected_encode(_value):
        nonlocal encoded
        encoded = True
        raise AssertionError("oversized FIT must not be Base64 encoded")

    monkeypatch.setattr(base64, "b64encode", unexpected_encode)
    try:
        response = await query_workouts(
            "download",
            42,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    assert json.loads(response)["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert encoded is False


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

    assert ("get_available_badge_challenges", (0, 51), {}) in client.calls
    assert ("get_non_completed_badge_challenges", (0, 51), {}) in client.calls
    assert ("get_badge_challenges", (0, 51), {}) in client.calls
    assert ("get_adhoc_challenges", (0, 51), {}) in client.calls
    assert ("get_inprogress_virtual_challenges", (1, 51), {}) in client.calls
    assert all(call[0] != "get_earned_badges" for call in client.calls)


@pytest.mark.asyncio
async def test_challenge_page_trims_actual_nested_collections_to_the_item_limit():
    client = RecordingClient(
        {
            "get_available_badge_challenges": {
                "first": [{"name": f"challenge-{item}"} for item in range(40)],
                "second": [{"name": f"challenge-{item}"} for item in range(40, 50)],
                "totalCount": 61,
            }
        }
    )

    budget = RequestBudget("query_challenges", policy_for_surface("query_challenges"))
    token = set_current_request_budget(budget)
    try:
        response = json.loads(
            await query_challenges(
                status="available",
                challenge_type="badge",
                ctx=FakeContext(client),  # type: ignore[arg-type]
            )
        )
    finally:
        reset_current_request_budget(token)

    available = response["data"]["available_badges"]
    assert len(available) == 50
    assert response["metadata"]["category_counts"]["available_badges"] == 50
    assert response["pagination"]["has_more"] is True
    assert "partial" not in response["pagination"]
    assert policy_for_surface("query_challenges").partial_results_allowed is False
    assert budget.truncation_reason is None
    assert budget.continuation_emitted is True

    nested, count, has_more = _normalize_challenge_page(
        {
            "first": [{"name": str(item)} for item in range(40)],
            "second": [{"name": str(item)} for item in range(40, 61)],
        },
        50,
    )
    assert len(nested) == 50
    assert count == 50
    assert has_more is True


@pytest.mark.asyncio
async def test_oversized_challenge_page_fails_closed_without_partial_cursor():
    policy = policy_for_surface("query_challenges")
    client = RecordingClient(
        {
            "get_available_badge_challenges": {
                "items": [{"name": "x" * (policy.max_response_bytes + 1)}],
                "totalCount": 1,
            }
        }
    )
    budget = RequestBudget("query_challenges", policy)
    token = set_current_request_budget(budget)
    try:
        response = json.loads(
            await query_challenges(
                status="available",
                challenge_type="badge",
                ctx=FakeContext(client),  # type: ignore[arg-type]
            )
        )
    finally:
        reset_current_request_budget(token)

    assert response["error"]["code"] == "RESPONSE_BUDGET_EXCEEDED"
    assert "pagination" not in response
    assert policy.partial_results_allowed is False
    assert budget.truncation_reason == "response_bytes"
    assert budget.continuation_emitted is False


@pytest.mark.asyncio
async def test_short_challenge_page_with_more_items_fails_closed_instead_of_skipping_offsets():
    client = RecordingClient(
        {
            "get_available_badge_challenges": {
                "items": [{"name": str(item)} for item in range(40)],
                "totalCount": 61,
            }
        }
    )

    response = json.loads(
        await query_challenges(
            status="available",
            challenge_type="badge",
            limit=50,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert response["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"
    assert "pagination" not in response


@pytest.mark.asyncio
async def test_short_challenge_page_uses_actual_count_to_detect_a_hidden_remainder():
    client = RecordingClient(
        {
            "get_available_badge_challenges": {
                "items": [{"name": str(item)} for item in range(40)],
                "totalCount": 50,
            }
        }
    )

    response = json.loads(
        await query_challenges(
            status="available",
            challenge_type="badge",
            limit=50,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert response["error"]["code"] == "GARMIN_UPSTREAM_UNAVAILABLE"


def test_find_similar_policy_matches_the_public_default_and_maximum():
    policy = policy_for_surface("find_similar_activities")

    assert policy.default_page_items == 10
    assert policy.max_page_items == 20

    challenge_policy = policy_for_surface("query_challenges")
    assert challenge_policy.default_page_items == 50
    assert challenge_policy.max_page_items == 50
    assert challenge_policy.max_aggregate_items == 250


@pytest.mark.asyncio
async def test_challenge_cursor_and_page_size_errors_are_stable_json():
    context = FakeContext(RecordingClient())
    malformed = json.loads(
        await query_challenges(cursor="malformed!", ctx=context)  # type: ignore[arg-type]
    )
    assert malformed["error"]["code"] == "INVALID_CONTINUATION_CURSOR"

    wrong_filter_cursor = encode_continuation_cursor(
        surface="query_challenges",
        position=50,
        page_size=50,
        filters={"status": "active", "challenge_type": "badge"},
    )
    changed = json.loads(
        await query_challenges(
            status="available",
            challenge_type="badge",
            cursor=wrong_filter_cursor,
            ctx=context,  # type: ignore[arg-type]
        )
    )
    assert changed["error"]["code"] == "INVALID_CONTINUATION_CURSOR"

    invalid_limit = json.loads(
        await query_challenges(limit=51, ctx=context)  # type: ignore[arg-type]
    )
    assert invalid_limit["error"]["code"] == "INVALID_PAGE_SIZE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected", "excluded"),
    [
        ("available", "get_available_badge_challenges", "get_badge_challenges"),
        ("active", "get_non_completed_badge_challenges", "get_badge_challenges"),
        ("earned", "get_badge_challenges", "get_non_completed_badge_challenges"),
    ],
)
async def test_challenge_status_dispatches_only_matching_badge_category(
    status: str,
    expected: str,
    excluded: str,
):
    client = RecordingClient()

    await query_challenges(
        status=status,
        challenge_type="badge",
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )

    methods = {call[0] for call in client.calls}
    assert expected in methods
    assert excluded not in methods


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "available", "earned"])
async def test_historical_adhoc_endpoint_is_not_used_for_narrow_statuses(status: str):
    client = RecordingClient()

    response = json.loads(
        await query_challenges(
            status=status,
            challenge_type="adhoc",
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert client.calls == []
    assert response["data"]["adhoc_challenges"] is None
    assert "adhoc_challenges" in response["metadata"]["unavailable"]


@pytest.mark.asyncio
async def test_cursor_overflow_is_rejected_before_workout_dispatch():
    client = RecordingClient({"get_workouts": []})
    cursor = encode_continuation_cursor(
        surface="query_workouts",
        position=999_950,
        page_size=50,
        filters={"action": "list"},
    )

    response = json.loads(
        await query_workouts(
            "list",
            cursor=cursor,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert response["error"]["code"] == "INVALID_CONTINUATION_CURSOR"
    assert client.calls == []


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

    result = await query_gear(
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
    assert "user_profile_number" not in json.loads(result)["metadata"]


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
        def get_read_client(self, preflight=None) -> RecordingClient:
            if preflight is not None:
                preflight()
            return client

    monkeypatch.setattr(session, "get_session_manager", lambda: Manager())
    monkeypatch.setattr(server, "get_today_date_string", lambda: "2026-07-19")

    await server.athlete_profile_resource()
    await server.training_readiness_resource()
    await server.health_today_resource()

    assert ("get_user_summary", ("2026-07-19",), {}) in client.calls
    assert client.calls.count(("get_stats", ("2026-07-19",), {})) == 2
    assert ("get_training_readiness", ("2026-07-19",), {}) in client.calls


@pytest.mark.asyncio
async def test_training_period_projection_preserves_observable_breakdowns():
    activities = [
        {
            "activityId": 1,
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-07-02T08:00:00",
            "distance": 5000,
            "duration": 1500,
            "elevationGain": 25,
        },
        {
            "activityId": 2,
            "activityType": {"typeKey": "running"},
            "startTimeLocal": "2026-07-09T08:00:00",
            "distance": 10000,
            "duration": 3300,
            "elevationGain": 50,
        },
    ]
    client = RecordingClient({"get_activities": activities})

    result = await analyze_training_period(
        "2026-07-01:2026-07-14",
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )
    data = json.loads(result)["data"]

    assert data["summary"]["averages"]["distance_per_activity"]["meters"] == 7500
    assert data["by_activity_type"][0]["type"] == "running"
    assert len(data["trends"]["weekly"]) == 3


@pytest.mark.asyncio
async def test_max_training_range_reserves_all_54_calendar_week_buckets():
    client = RecordingClient(
        {
            "get_activities": [
                {
                    "activityId": 1,
                    "activityType": {"typeKey": "running"},
                    "startTimeLocal": "2023-06-01T08:00:00",
                    "distance": 1000,
                    "duration": 300,
                }
            ]
        }
    )
    policy = policy_for_surface("analyze_training_period")
    budget = RequestBudget("analyze_training_period", policy)
    token = set_current_request_budget(budget)
    try:
        result = await analyze_training_period(
            "2023-01-01:2024-01-01",
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    finally:
        reset_current_request_budget(token)

    payload = json.loads(result)
    assert len(payload["data"]["trends"]["weekly"]) == 54
    assert policy.max_page_items == 54
    assert policy.max_aggregate_items == 108
    assert budget.items_used == 55


@pytest.mark.asyncio
async def test_training_period_accounts_for_weekly_and_activity_type_collections():
    activities = [
        {
            "activityId": item,
            "activityType": {"typeKey": f"type-{item}"},
            "startTimeLocal": "2026-07-01T08:00:00",
            "distance": 1000,
            "duration": 300,
        }
        for item in range(55)
    ]

    class PagedTrainingClient(RecordingClient):
        async def call(self, method_name: str, *args: object, **kwargs: object) -> object:
            self.calls.append((method_name, args, kwargs))
            assert method_name == "get_activities"
            offset, limit = int(args[0]), int(args[1])
            return activities[offset : offset + limit]

    policy = policy_for_surface("analyze_training_period")
    budget = RequestBudget("analyze_training_period", policy)
    token = set_current_request_budget(budget)
    try:
        response = json.loads(
            await analyze_training_period(
                "2026-07-01:2026-07-01",
                ctx=FakeContext(PagedTrainingClient()),  # type: ignore[arg-type]
            )
        )
    finally:
        reset_current_request_budget(token)

    type_count = len(response["data"]["by_activity_type"])
    week_count = len(response["data"]["trends"]["weekly"])
    assert type_count == 55
    assert budget.items_used == type_count + week_count
    assert policy.max_aggregate_items is not None
    assert budget.items_used <= policy.max_aggregate_items


@pytest.mark.asyncio
async def test_near_ceiling_workout_cursor_is_rejected_before_upstream_call():
    policy = policy_for_surface("query_workouts")
    cursor = encode_continuation_cursor(
        surface="query_workouts",
        position=MAX_CONTINUATION_POSITION - policy.max_page_items - 1,
        page_size=policy.max_page_items,
        filters={"action": "list"},
    )
    client = RecordingClient({"get_workouts": []})

    response = json.loads(
        await query_workouts(
            action="list",
            cursor=cursor,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert response["error"]["code"] == "INVALID_CONTINUATION_CURSOR"
    assert client.calls == []


@pytest.mark.asyncio
async def test_server_never_emits_a_cursor_that_its_decoder_would_reject():
    policy = policy_for_surface("query_workouts")
    position = MAX_CONTINUATION_POSITION - (2 * policy.max_page_items) - 1
    cursor = encode_continuation_cursor(
        surface="query_workouts",
        position=position,
        page_size=policy.max_page_items,
        filters={"action": "list"},
    )
    client = RecordingClient(
        {"get_workouts": [{"workoutId": item} for item in range(policy.max_page_items + 1)]}
    )

    response = json.loads(
        await query_workouts(
            action="list",
            cursor=cursor,
            ctx=FakeContext(client),  # type: ignore[arg-type]
        )
    )

    assert response["error"]["code"] == "INVALID_CONTINUATION_CURSOR"
    assert "pagination" not in response
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_similar_activity_tool_preserves_all_documented_differences():
    reference = {
        "activityId": 1,
        "activityType": {"typeKey": "running"},
        "distance": 5000,
        "duration": 1500,
        "elevationGain": 100,
    }
    candidate = {
        "activityId": 2,
        "activityType": {"typeKey": "running"},
        "distance": 5100,
        "duration": 1530,
        "elevationGain": 105,
    }
    client = RecordingClient({"get_activity": reference, "get_activities": [reference, candidate]})

    result = await find_similar_activities(
        1,
        criteria="type,distance,elevation,duration",
        ctx=FakeContext(client),  # type: ignore[arg-type]
    )
    differences = json.loads(result)["data"]["similar_activities"][0]["differences"]

    assert set(differences) == {"type", "distance", "elevation", "duration"}


def test_relative_dates_follow_the_server_local_timezone():
    instant = datetime(2026, 7, 18, 22, 30, tzinfo=UTC)
    moscow = instant.astimezone(timezone(timedelta(hours=3)))
    new_york = instant.astimezone(timezone(timedelta(hours=-4)))

    assert get_today_date_string(now=moscow) == "2026-07-19"
    assert get_today_date_string(now=new_york) == "2026-07-18"
    assert parse_date_string("today", now=moscow).strftime("%Y-%m-%d") == "2026-07-19"
    assert parse_date_string("yesterday", now=moscow).strftime("%Y-%m-%d") == "2026-07-18"
