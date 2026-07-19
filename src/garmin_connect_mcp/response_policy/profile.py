"""Public projections for profile, gear, goals, challenges, and workouts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import project_list, project_object, project_tree
from .devices import project_device
from .health import project_health_payload

PROFILE_FIELDS = frozenset(
    {
        "full_name",
        "name",
        "displayName",
        "fullName",
        "unit_system",
        "measurementSystem",
        "preferredLocale",
        "timeFormat",
        "dateFormat",
        "firstDayOfWeek",
    }
)

GOAL_FIELDS = frozenset(
    {
        "goalTypeId",
        "goalTypeName",
        "goalValue",
        "targetValue",
        "currentValue",
        "percentageComplete",
        "startDate",
        "endDate",
        "calendarDate",
        "activityType",
        "typeId",
        "typeKey",
        "name",
        "description",
        "status",
        "progress",
        "value",
        "unit",
    }
)

PERSONAL_RECORD_FIELDS = frozenset(
    {
        "activityId",
        "activityName",
        "activityType",
        "typeId",
        "typeKey",
        "calendarDate",
        "date",
        "distance",
        "duration",
        "pace",
        "value",
        "unit",
        "recordType",
        "recordTypeKey",
        "name",
        "displayName",
    }
)

RACE_PREDICTION_FIELDS = frozenset(
    {
        "predictionType",
        "predictedTime",
        "distance",
        "pace",
        "value",
        "unit",
        "calendarDate",
        "date",
        "name",
    }
)

CHALLENGE_FIELDS = frozenset(
    {
        "name",
        "displayName",
        "description",
        "status",
        "progress",
        "points",
        "earnedDate",
        "joinDate",
        "completedDate",
        "startDate",
        "endDate",
        "participantCount",
        "typeId",
        "typeKey",
        "percentageComplete",
        "targetValue",
        "currentValue",
        "value",
        "unit",
    }
)

GEAR_FIELDS = frozenset(
    {
        "gearPk",
        "uuid",
        "name",
        "displayName",
        "productDisplayName",
        "customMakeModel",
        "gearStatusName",
        "dateBegin",
        "dateEnd",
        "maximumMeters",
        "totalDistance",
        "notified",
        "defaultGear",
        "activityType",
        "typeId",
        "typeKey",
        "distance",
        "value",
        "unit",
    }
)

WORKOUT_FIELDS = frozenset(
    {
        "workoutId",
        "workoutName",
        "name",
        "description",
        "sportType",
        "typeId",
        "typeKey",
        "estimatedDurationInSecs",
        "estimatedDistanceInMeters",
        "workoutSegments",
        "workoutSteps",
        "segmentOrder",
        "stepOrder",
        "stepType",
        "intensity",
        "targetType",
        "targetValueOne",
        "targetValueTwo",
        "durationType",
        "durationValue",
        "exerciseName",
        "repetitionCount",
        "category",
        "success",
    }
)


def project_profile(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    def project_profile_block(item: Any) -> dict[str, Any]:
        return project_object(
            item,
            scalar_fields=frozenset({"full_name", "name", "unit_system"}),
            field_projectors={"settings": lambda nested: project_tree(nested, PROFILE_FIELDS)},
        )

    return project_object(
        value,
        field_projectors={
            "profile": project_profile_block,
            "stats": project_health_payload,
            "user_summary": project_health_payload,
            "summary": project_health_payload,
            "personal_records": lambda item: project_tree(item, PERSONAL_RECORD_FIELDS),
            "devices": lambda item: project_list(item, project_device),
            "primary_device": project_device,
        },
    )


def project_athlete_resource(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    return project_profile(value)


def project_goals_records(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "goals": lambda item: project_tree(item, GOAL_FIELDS),
            "personal_records": lambda item: project_tree(item, PERSONAL_RECORD_FIELDS),
            "race_predictions": lambda item: project_tree(item, RACE_PREDICTION_FIELDS),
        },
    )


def project_challenges(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: project_tree(item, CHALLENGE_FIELDS)
        for key, item in value.items()
        if key
        in {
            "available_badges",
            "active_badges",
            "earned_badges",
            "all_badge_challenges",
            "adhoc_challenges",
            "active_virtual_challenges",
        }
    }


def project_gear(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "gear": lambda item: project_tree(item, GEAR_FIELDS),
            "defaults": lambda item: project_tree(item, GEAR_FIELDS),
            "stats": lambda item: project_tree(item, GEAR_FIELDS),
        },
    )


def project_workouts(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        scalar_fields=frozenset({"count"}),
        field_projectors={
            "workouts": lambda item: project_tree(item, WORKOUT_FIELDS),
            "workout": lambda item: project_tree(item, WORKOUT_FIELDS),
            "workout_file": lambda item: project_tree(
                item,
                frozenset({"content_base64", "content_type", "encoding", "sha256", "size_bytes"}),
            ),
        },
    )
