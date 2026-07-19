"""Privacy behavior for device payloads."""

import json

from garmin_connect_mcp.response_builder import ResponseBuilder


def test_device_projection_keeps_operational_id_and_drops_stable_identifiers():
    response = ResponseBuilder.build_response(
        {
            "devices": [
                {
                    "deviceId": 123,
                    "displayName": "Forerunner",
                    "softwareVersion": "20.10",
                    "batteryStatus": "GOOD",
                    "serialNumber": "CANARY_SERIAL",
                    "unitId": "CANARY_UNIT",
                    "userProfileId": "CANARY_OWNER",
                }
            ],
            "device_settings": {
                "deviceId": 123,
                "settingName": "timeFormat",
                "value": "24h",
                "oauthToken": "CANARY_SECRET",
            },
        },
        surface="query_devices",
    )
    payload = json.loads(response)

    assert payload["data"]["devices"] == [
        {
            "deviceId": 123,
            "displayName": "Forerunner",
            "softwareVersion": "20.10",
            "batteryStatus": "GOOD",
        }
    ]
    assert payload["data"]["device_settings"] == {
        "deviceId": 123,
        "settingName": "timeFormat",
        "value": "24h",
    }
    assert "CANARY" not in response


def test_device_detail_subtypes_are_projected_separately():
    response = ResponseBuilder.build_response(
        {
            "device_settings": {
                "settingName": "timeFormat",
                "value": "24h",
                "solarIntensity": "CANARY_SOLAR",
                "repeat": "CANARY_ALARM",
            },
            "solar_data": [
                {
                    "timestamp": 1,
                    "solarIntensity": 80,
                    "settingName": "CANARY_SETTING",
                    "repeat": "CANARY_ALARM",
                }
            ],
            "alarms": [
                {
                    "time": "07:00",
                    "enabled": True,
                    "solarIntensity": "CANARY_SOLAR",
                    "settingName": "CANARY_SETTING",
                }
            ],
        },
        surface="query_devices",
    )
    payload = json.loads(response)

    assert payload["data"] == {
        "device_settings": {"settingName": "timeFormat", "value": "24h"},
        "solar_data": [{"timestamp": 1, "solarIntensity": 80}],
        "alarms": [{"time": "07:00", "enabled": True}],
    }
    assert "CANARY" not in response
