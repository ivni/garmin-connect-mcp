"""Public projections for activity and activity-analysis responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import project_list, project_object, project_tree

ACTIVITY_FIELDS = frozenset(
    {
        "activityId",
        "activityName",
        "description",
        "activityType",
        "eventType",
        "startTimeLocal",
        "startTimeGMT",
        "endTimeLocal",
        "distance",
        "duration",
        "movingDuration",
        "elapsedDuration",
        "elevationGain",
        "elevationLoss",
        "minElevation",
        "maxElevation",
        "averageSpeed",
        "maxSpeed",
        "averageHR",
        "maxHR",
        "heart_rate",
        "avgPower",
        "maxPower",
        "normalizedPower",
        "power",
        "avgRunCadence",
        "maxRunCadence",
        "averageBikingCadenceInRevPerMinute",
        "maxBikingCadenceInRevPerMinute",
        "cadence",
        "calories",
        "steps",
        "aerobicTrainingEffect",
        "anaerobicTrainingEffect",
        "activityTrainingLoad",
        "trainingEffectLabel",
        "vO2MaxValue",
        "avgRespirationRate",
        "maxRespirationRate",
        "minRespirationRate",
        "lapCount",
        "waterEstimated",
        "moderateIntensityMinutes",
        "vigorousIntensityMinutes",
        "typeId",
        "typeKey",
        "parentTypeId",
        "sortOrder",
        "meters",
        "seconds",
        "mps",
        "avg_bpm",
        "max_bpm",
        "avg_watts",
        "max_watts",
        "avg_spm",
        "avg_rpm",
        "formatted",
        "formatted_speed",
        "formatted_pace",
        "datetime",
        "date",
        "day_of_week",
    }
)

LOCATION_INPUT_FIELDS = {
    "startLatitude": "start_latitude",
    "startLongitude": "start_longitude",
    "endLatitude": "end_latitude",
    "endLongitude": "end_longitude",
    "latitude": "latitude",
    "longitude": "longitude",
    "locationName": "name",
}

SPLIT_FIELDS = frozenset(
    {
        "lapDTOs",
        "laps",
        "splits",
        "split_number",
        "lapIndex",
        "startTimeGMT",
        "startTimeLocal",
        "distance",
        "distance_meters",
        "distance_formatted",
        "duration",
        "time_seconds",
        "cumulative_time_seconds",
        "time_formatted",
        "pace_formatted",
        "averageSpeed",
        "maxSpeed",
        "averageHR",
        "maxHR",
        "avgPower",
        "maxPower",
        "calories",
        "elevationGain",
        "elevationLoss",
        "intensityType",
        "formatted",
        "meters",
        "seconds",
        "mps",
    }
)

COMPUTED_SPLIT_FIELDS = SPLIT_FIELDS | frozenset(
    {
        "partial",
        "accurate",
        "estimated",
        "note",
        "reason",
        "average_pace",
        "seconds_per_km",
        "total_distance_meters",
        "total_duration_seconds",
        "data_points",
    }
)

HR_ZONE_FIELDS = frozenset(
    {
        "zoneNumber",
        "zoneLowBoundary",
        "secsInZone",
        "zoneHighBoundary",
    }
)

WEATHER_FIELDS = frozenset(
    {
        "weatherTypeDTO",
        "desc",
        "temperature",
        "apparentTemperature",
        "dewPoint",
        "relativeHumidity",
        "windDirection",
        "windDirectionCompassPoint",
        "windSpeed",
        "windGust",
        "weatherStationDTO",
        "name",
        "reportType",
    }
)

GEAR_DETAIL_FIELDS = frozenset(
    {
        "gearPk",
        "uuid",
        "displayName",
        "customMakeModel",
        "gearStatusName",
        "dateBegin",
        "dateEnd",
        "maximumMeters",
        "notified",
        "totalDistance",
    }
)

EXERCISE_SET_FIELDS = frozenset(
    {
        "exerciseName",
        "category",
        "repetitionCount",
        "weight",
        "weightUnit",
        "setType",
    }
)

DERIVED_FIELDS = frozenset(
    {
        "count",
        "total_distance",
        "total_time",
        "total_elevation",
        "total_calories",
        "avg_distance_per_activity",
        "avg_speed",
        "distance",
        "time",
        "duration",
        "pace",
        "elevation",
        "heart_rate",
        "longest",
        "shortest",
        "fastest",
        "slowest",
        "most",
        "least",
        "highest_avg",
        "lowest_avg",
        "id",
        "meters",
        "seconds",
        "mps",
        "bpm",
        "formatted",
        "formatted_speed",
        "formatted_pace",
        "similarity_score",
        "differences",
        "type",
        "match",
        "reference",
        "activity",
        "diff_meters",
        "diff_seconds",
        "diff_percent",
        "within_tolerance",
    }
)


def project_activity(value: Any, *, include_location: bool = False) -> Any:
    """Project one activity and optionally expose exact location."""
    if value is None:
        return None
    projected = project_tree(value, ACTIVITY_FIELDS)
    if not isinstance(projected, dict):
        projected = {}
    if include_location and isinstance(value, Mapping):
        location = {
            public: value[upstream]
            for upstream, public in LOCATION_INPUT_FIELDS.items()
            if upstream in value and value[upstream] is not None
        }
        if location:
            projected["location"] = location
    return projected


def _include_location(context: Mapping[str, object] | None) -> bool:
    return bool(context and context.get("include_location") is True)


def project_activity_query(
    value: Any, context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    include_location = _include_location(context)
    return project_object(
        value,
        scalar_fields=frozenset({"count"}),
        field_projectors={
            "activity": lambda item: project_activity(item, include_location=include_location),
            "activities": lambda item: project_list(
                item, lambda entry: project_activity(entry, include_location=include_location)
            ),
            "aggregated": lambda item: project_tree(item, DERIVED_FIELDS),
        },
    )


def project_activity_details(
    value: Any, context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    include_location = _include_location(context)
    return project_object(
        value,
        field_projectors={
            "activity": lambda item: project_activity(item, include_location=include_location),
            "splits": lambda item: project_tree(item, SPLIT_FIELDS),
            "computed_splits": lambda item: project_tree(item, COMPUTED_SPLIT_FIELDS),
            "weather": lambda item: project_tree(item, WEATHER_FIELDS),
            "hr_zones": lambda item: project_tree(item, HR_ZONE_FIELDS),
            "gear": lambda item: project_tree(item, GEAR_DETAIL_FIELDS),
            "exercise_sets": lambda item: project_tree(item, EXERCISE_SET_FIELDS),
        },
    )


def project_comparison(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        scalar_fields=frozenset({"count"}),
        field_projectors={
            "activities": lambda item: project_list(item, project_activity),
            "comparison": lambda item: project_tree(item, DERIVED_FIELDS),
        },
    )


def project_similar(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    def project_match(item: Any) -> dict[str, Any]:
        return project_object(
            item,
            scalar_fields=frozenset({"similarity_score"}),
            field_projectors={
                "activity": project_activity,
                "differences": lambda nested: project_tree(nested, DERIVED_FIELDS),
            },
        )

    return project_object(
        value,
        scalar_fields=frozenset({"count"}),
        field_projectors={
            "reference_activity": project_activity,
            "similar_activities": lambda item: project_list(item, project_match),
        },
    )


def project_social(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Future-proof external social text without treating it as trusted instruction."""

    def project_comment(comment: Any) -> dict[str, Any]:
        projected = project_object(
            comment,
            scalar_fields=frozenset({"text", "created_at"}),
            field_projectors={
                "author": lambda author: project_tree(author, frozenset({"display_name"}))
            },
        )
        projected["content_origin"] = "external_garmin_user"
        projected["trusted"] = False
        return projected

    return project_object(
        value,
        scalar_fields=frozenset({"count"}),
        field_projectors={"comments": lambda comments: project_list(comments, project_comment)},
    )
