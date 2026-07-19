"""Privacy behavior for activity summaries and details."""

from __future__ import annotations

import inspect
import json

from garmin_connect_mcp.response_builder import ResponseBuilder
from garmin_connect_mcp.response_policy import project_surface_data
from garmin_connect_mcp.tools.activities import get_activity_details, query_activities


def _activity() -> dict[str, object]:
    return {
        "activityId": 42,
        "activityName": "Morning Run",
        "activityType": {"typeId": 1, "typeKey": "running", "ownerId": "CANARY_OWNER"},
        "distance": {"meters": 5000, "formatted": "5.00 km", "secret": "CANARY_SECRET"},
        "duration": {"seconds": 1800, "formatted": "30m 0s"},
        "startLatitude": 55.7512,
        "startLongitude": 37.6184,
        "endLatitude": 55.7520,
        "endLongitude": 37.6200,
        "locationName": "Home route",
        "deviceId": "CANARY_DEVICE",
        "ownerId": "CANARY_OWNER",
        "serialNumber": "CANARY_SERIAL",
        "di_refresh_token": "CANARY_SECRET",
        "unknown": {"nested": "CANARY_SECRET"},
    }


def test_activity_default_omits_exact_location_and_unrelated_identifiers():
    response = ResponseBuilder.build_response(
        {"activities": [_activity()], "count": 1},
        surface="query_activities",
        policy_context={"include_location": False},
    )
    payload = json.loads(response)
    activity = payload["data"]["activities"][0]

    assert activity["activityId"] == 42
    assert activity["activityType"] == {"typeId": 1, "typeKey": "running"}
    assert activity["distance"] == {"meters": 5000, "formatted": "5.00 km"}
    assert "location" not in activity
    assert "CANARY" not in response


def test_activity_location_requires_explicit_per_call_opt_in():
    payload = json.loads(
        ResponseBuilder.build_response(
            {"activity": _activity()},
            surface="query_activities",
            policy_context={"include_location": True},
        )
    )

    assert payload["data"]["activity"]["location"] == {
        "start_latitude": 55.7512,
        "start_longitude": 37.6184,
        "end_latitude": 55.752,
        "end_longitude": 37.62,
        "name": "Home route",
    }
    assert payload["metadata"]["precise_location"] is True
    assert "CANARY_DEVICE" not in json.dumps(payload)


def test_activity_location_metadata_is_absent_without_returned_location():
    payload = json.loads(
        ResponseBuilder.build_response(
            {"activity": {"activityId": 42}},
            surface="query_activities",
            policy_context={"include_location": True},
        )
    )

    assert "precise_location" not in payload["metadata"]


def test_activity_details_project_nested_optional_payloads():
    response = ResponseBuilder.build_response(
        {
            "activity": _activity(),
            "weather": {
                "temperature": 18,
                "relativeHumidity": 60,
                "stationOwnerId": "CANARY_OWNER",
            },
            "splits": {"lapDTOs": [{"lapIndex": 1, "distance": 1000, "ownerId": "CANARY_OWNER"}]},
        },
        surface="get_activity_details",
    )
    payload = json.loads(response)

    assert payload["data"]["weather"] == {"temperature": 18, "relativeHumidity": 60}
    assert payload["data"]["splits"]["lapDTOs"] == [{"lapIndex": 1, "distance": 1000}]
    assert "CANARY" not in response


def test_activity_detail_subtypes_cannot_cross_domain_boundaries():
    payload = json.loads(
        ResponseBuilder.build_response(
            {
                "weather": {
                    "temperature": 18,
                    "gearPk": "CANARY_GEAR",
                    "exerciseName": "CANARY_EXERCISE",
                    "weatherTypePk": "CANARY_TYPE_ID",
                    "weatherStationPk": "CANARY_STATION_ID",
                    "instrumentNo": "CANARY_INSTRUMENT_ID",
                },
                "hr_zones": [
                    {"zoneNumber": 1, "temperature": "CANARY_WEATHER", "gearPk": "CANARY"}
                ],
                "gear": [{"gearPk": 7, "displayName": "Shoes", "temperature": "CANARY"}],
                "exercise_sets": [
                    {"exerciseName": "Squat", "gearPk": "CANARY", "temperature": "CANARY"}
                ],
            },
            surface="get_activity_details",
        )
    )

    assert payload["data"] == {
        "weather": {"temperature": 18},
        "hr_zones": [{"zoneNumber": 1}],
        "gear": [{"gearPk": 7, "displayName": "Shoes"}],
        "exercise_sets": [{"exerciseName": "Squat"}],
    }
    assert "CANARY" not in json.dumps(payload)


def test_similar_activity_projection_keeps_duration_differences():
    projected = project_surface_data(
        "find_similar_activities",
        {
            "similar_activities": [
                {
                    "activity": {"activityId": 42},
                    "similarity_score": 0.9,
                    "differences": {
                        "type": {"match": True},
                        "distance": {"diff_meters": 10},
                        "elevation": {"diff_meters": 2},
                        "duration": {"diff_seconds": 5},
                    },
                }
            ]
        },
    )

    assert set(projected["similar_activities"][0]["differences"]) == {
        "type",
        "distance",
        "elevation",
        "duration",
    }


def test_activity_tools_expose_the_documented_location_flag():
    assert inspect.signature(query_activities).parameters["include_location"].default is False
    assert inspect.signature(get_activity_details).parameters["include_location"].default is False
