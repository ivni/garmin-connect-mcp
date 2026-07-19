"""Public projections for Garmin devices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import project_list, project_object, project_tree

DEVICE_FIELDS = frozenset(
    {
        "deviceId",
        "displayName",
        "productDisplayName",
        "productName",
        "productNumber",
        "deviceTypePk",
        "deviceStatusName",
        "deviceStatus",
        "primary",
        "trainingDevice",
        "softwareVersion",
        "batteryStatus",
        "batteryLevel",
        "lastSyncTime",
        "lastUsed",
        "imageUrl",
    }
)

DEVICE_SETTING_FIELDS = frozenset(
    {
        "deviceId",
        "settingName",
        "displayName",
        "value",
        "enabled",
    }
)

SOLAR_FIELDS = frozenset(
    {
        "deviceId",
        "calendarDate",
        "timestamp",
        "solarIntensity",
        "duration",
        "batteryStatus",
        "batteryLevel",
    }
)

ALARM_FIELDS = frozenset(
    {
        "deviceId",
        "displayName",
        "enabled",
        "time",
        "repeat",
        "days",
    }
)


def project_device(value: Any) -> Any:
    if value is None:
        return None
    projected = project_tree(value, DEVICE_FIELDS)
    return projected if isinstance(projected, dict) else {}


def project_devices(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "devices": lambda item: project_list(item, project_device),
            "last_used": project_device,
            "primary_device": project_device,
            "device_settings": lambda item: project_tree(item, DEVICE_SETTING_FIELDS),
            "solar_data": lambda item: project_tree(item, SOLAR_FIELDS),
            "alarms": lambda item: project_tree(item, ALARM_FIELDS),
        },
    )
