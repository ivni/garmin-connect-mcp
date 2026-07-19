"""Fail-closed projection for health and reproductive-health payloads."""

import json

from garmin_connect_mcp.response_builder import ResponseBuilder


def test_health_projection_drops_unknown_top_level_and_nested_fields():
    response = ResponseBuilder.build_response(
        {
            "date": {
                "date": "2026-07-19",
                "formatted": "Sunday, July 19, 2026",
                "unknown": "CANARY_SECRET",
            },
            "stats": {
                "totalSteps": 9000,
                "restingHeartRate": 50,
                "bodyBatteryValuesArray": [
                    {"timestamp": 1, "value": 80, "deviceId": "CANARY_DEVICE"}
                ],
                "userProfileId": "CANARY_OWNER",
            },
        },
        surface="query_health_summary",
    )
    payload = json.loads(response)

    assert payload["data"]["date"] == {
        "date": "2026-07-19",
        "formatted": "Sunday, July 19, 2026",
    }
    assert payload["data"]["stats"] == {
        "totalSteps": 9000,
        "restingHeartRate": 50,
        "bodyBatteryValuesArray": [{"timestamp": 1, "value": 80}],
    }
    assert "CANARY" not in response


def test_reproductive_health_is_available_only_through_its_explicit_surface():
    reproductive = {
        "pregnancy_summary": {
            "dueDate": "2027-01-01",
            "currentWeek": 12,
            "userProfileId": "CANARY_OWNER",
        }
    }
    explicit = ResponseBuilder.build_response(reproductive, surface="query_womens_health")
    general = ResponseBuilder.build_response(
        {"health": {"totalSteps": 1000, **reproductive}},
        surface="garmin://health/today",
    )

    assert json.loads(explicit)["data"]["pregnancy_summary"] == {
        "dueDate": "2027-01-01",
        "currentWeek": 12,
    }
    assert "pregnancy_summary" not in general
    assert "CANARY" not in explicit


def test_response_schema_is_present_on_success_responses():
    payload = json.loads(
        ResponseBuilder.build_response(
            {"health": {"totalSteps": 1000}}, surface="garmin://health/today"
        )
    )
    assert payload["metadata"]["response_schema"] == "2"


def test_health_endpoint_projectors_keep_known_typed_fields():
    sleep = json.loads(
        ResponseBuilder.build_response(
            {
                "sleep": {
                    "avgOvernightHrv": 48.5,
                    "sleepHeartRate": [[1, 52]],
                    "wellnessEpochRespirationDataDTOList": [
                        {"timestamp": 1, "respirationValue": 14.2}
                    ],
                }
            },
            surface="query_sleep_data",
        )
    )["data"]["sleep"]
    metrics = json.loads(
        ResponseBuilder.build_response(
            {
                "stress": {
                    "avgStressLevel": 22,
                    "stressValuesArray": [[1, 22]],
                },
                "steps": [{"steps": 100, "startGMT": "10:00", "endGMT": "10:15"}],
            },
            surface="query_activity_metrics",
        )
    )["data"]

    assert sleep == {
        "avgOvernightHrv": 48.5,
        "sleepHeartRate": [[1, 52]],
        "wellnessEpochRespirationDataDTOList": [{"timestamp": 1, "respirationValue": 14.2}],
    }
    assert metrics == {
        "stress": {"avgStressLevel": 22, "stressValuesArray": [[1, 22]]},
        "steps": [{"steps": 100, "startGMT": "10:00", "endGMT": "10:15"}],
    }


def test_performance_projection_rejects_cross_domain_health_fields():
    response = ResponseBuilder.build_response(
        {
            "vo2_max": {
                "vO2MaxValue": 52,
                "systolic": "CANARY_BP",
                "weight": "CANARY_WEIGHT",
            },
            "hrv": {"weeklyAvg": 48, "bodyFat": "CANARY_BODY"},
        },
        surface="get_performance_metrics",
    )
    payload = json.loads(response)

    assert payload["data"] == {
        "vo2_max": {"vO2MaxValue": 52},
        "hrv": {"weeklyAvg": 48},
    }
    assert "CANARY" not in response


def test_weight_projection_preserves_canonical_date_weight_list():
    response = ResponseBuilder.build_response(
        {
            "weigh_ins": {
                "dateWeightList": [
                    {
                        "calendarDate": "2026-07-19",
                        "weight": 75000,
                        "bmi": 22.5,
                        "samplePk": "CANARY_SAMPLE_ID",
                        "userProfileId": "CANARY_OWNER",
                    }
                ],
                "totalAverage": {"weight": 75000, "bmi": 22.5},
                "unknown": "CANARY_SECRET",
            },
            "date": "2026-07-19",
        },
        surface="query_weight_data",
    )
    payload = json.loads(response)

    assert payload["data"] == {
        "weigh_ins": {
            "dateWeightList": [{"calendarDate": "2026-07-19", "weight": 75000, "bmi": 22.5}],
            "totalAverage": {"weight": 75000, "bmi": 22.5},
        },
        "date": "2026-07-19",
    }
    assert "CANARY" not in response


def test_body_composition_metric_preserves_canonical_weight_container():
    response = ResponseBuilder.build_response(
        {
            "body_composition": {
                "dateWeightList": [
                    {
                        "calendarDate": "2026-07-19",
                        "weight": 75000,
                        "bodyFat": 15.5,
                        "userProfileId": "CANARY_OWNER",
                    }
                ],
                "totalAverage": {"weight": 75000, "bodyFat": 15.5},
            }
        },
        surface="query_activity_metrics",
    )
    payload = json.loads(response)

    assert payload["data"]["body_composition"] == {
        "dateWeightList": [{"calendarDate": "2026-07-19", "weight": 75000, "bodyFat": 15.5}],
        "totalAverage": {"weight": 75000, "bodyFat": 15.5},
    }
    assert "CANARY" not in response
