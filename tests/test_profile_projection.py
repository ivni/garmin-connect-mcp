"""Privacy behavior for profile and profile resources."""

import json

from garmin_connect_mcp.response_builder import ResponseBuilder


def test_profile_projection_replaces_raw_settings_with_reviewed_fields():
    response = ResponseBuilder.build_response(
        {
            "profile": {
                "full_name": "Athlete",
                "settings": {
                    "measurementSystem": "metric",
                    "timeFormat": "24-hour",
                    "email": "CANARY@example.test",
                    "userProfileId": "CANARY_OWNER",
                },
            },
            "stats": {"totalSteps": 8000, "userProfileId": "CANARY_OWNER"},
            "devices": [{"deviceId": 123, "displayName": "Watch", "serialNumber": "CANARY_SERIAL"}],
        },
        surface="get_user_profile",
    )
    payload = json.loads(response)

    assert payload["data"]["profile"] == {
        "full_name": "Athlete",
        "settings": {"measurementSystem": "metric", "timeFormat": "24-hour"},
    }
    assert payload["data"]["stats"] == {"totalSteps": 8000}
    assert payload["data"]["devices"] == [{"deviceId": 123, "displayName": "Watch"}]
    assert "CANARY" not in response


def test_athlete_resource_uses_the_same_profile_and_health_projection():
    response = ResponseBuilder.build_response(
        {
            "profile": {"name": "Athlete", "unit_system": "metric"},
            "summary": {"restingHeartRate": 48, "userProfileId": "CANARY_OWNER"},
            "stats": {"totalSteps": 10000, "unknown": "CANARY_SECRET"},
        },
        surface="garmin://athlete/profile",
    )
    payload = json.loads(response)

    assert payload["data"] == {
        "profile": {"name": "Athlete", "unit_system": "metric"},
        "summary": {"restingHeartRate": 48},
        "stats": {"totalSteps": 10000},
    }


def test_record_domains_do_not_share_unrelated_identifiers():
    workout = ResponseBuilder.build_response(
        {
            "workout": {
                "workoutId": 42,
                "workoutName": "Tempo",
                "challengeId": "CANARY_CHALLENGE",
                "gearPk": "CANARY_GEAR",
            }
        },
        surface="query_workouts",
    )
    challenge = ResponseBuilder.build_response(
        {
            "earned_badges": [
                {
                    "name": "Runner",
                    "challengeId": "CANARY_CHALLENGE",
                    "gearPk": "CANARY_GEAR",
                }
            ]
        },
        surface="query_challenges",
    )

    assert json.loads(workout)["data"]["workout"] == {
        "workoutId": 42,
        "workoutName": "Tempo",
    }
    assert json.loads(challenge)["data"]["earned_badges"] == [{"name": "Runner"}]
    assert "CANARY" not in workout
    assert "CANARY" not in challenge
