"""Executable response exposure policy and public projectors."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .activities import (
    project_activity_details,
    project_activity_query,
    project_comparison,
    project_similar,
    project_social,
)
from .common import project_write_response
from .contract import EXPOSURE_POLICIES, SurfaceExposurePolicy
from .devices import project_devices
from .health import (
    TRAINING_EFFECT_FIELDS,
    project_activity_metrics,
    project_health_resource,
    project_health_summary,
    project_heart_rate_response,
    project_performance,
    project_readiness_resource,
    project_sleep_response,
    project_weight,
    project_womens_health,
)
from .profile import (
    project_athlete_resource,
    project_challenges,
    project_gear,
    project_goals_records,
    project_profile,
    project_workouts,
)

SurfaceProjector = Callable[[Any, Mapping[str, object] | None], Any]


def _project_training_period(
    value: Any, context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    from .activities import DERIVED_FIELDS
    from .common import project_tree

    del context
    return project_tree(
        value,
        DERIVED_FIELDS
        | frozenset(
            {
                "period",
                "description",
                "days",
                "start_date",
                "end_date",
                "summary",
                "averages",
                "by_activity_type",
                "trends",
                "weekly",
                "type",
                "total_activities",
                "total_distance",
                "total_time",
                "total_elevation",
                "average_distance",
                "average_duration",
                "distance_per_activity",
                "activities_per_week",
                "count",
                "percentage",
                "activities",
                "distance",
                "time",
                "elevation",
                "week_start",
                "week_end",
            }
        ),
    )


def _project_training_effect(
    value: Any, context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    from .common import project_tree

    del context
    return project_tree(value, TRAINING_EFFECT_FIELDS)


PROJECTORS: dict[str, SurfaceProjector] = {
    "query_activities": project_activity_query,
    "get_activity_details": project_activity_details,
    "get_activity_social": project_social,
    "compare_activities": project_comparison,
    "find_similar_activities": project_similar,
    "query_health_summary": project_health_summary,
    "query_sleep_data": project_sleep_response,
    "query_heart_rate_data": project_heart_rate_response,
    "query_activity_metrics": project_activity_metrics,
    "query_devices": project_devices,
    "query_gear": project_gear,
    "get_user_profile": project_profile,
    "query_goals_and_records": project_goals_records,
    "query_challenges": project_challenges,
    "analyze_training_period": _project_training_period,
    "get_performance_metrics": project_performance,
    "get_training_effect": _project_training_effect,
    "query_weight_data": project_weight,
    "add_weight_entry": project_write_response,
    "delete_weight_entries": project_write_response,
    "query_workouts": project_workouts,
    "upload_workout": project_write_response,
    "log_body_composition": project_write_response,
    "log_blood_pressure": project_write_response,
    "log_hydration": project_write_response,
    "query_womens_health": project_womens_health,
    "garmin://athlete/profile": project_athlete_resource,
    "garmin://training/readiness": project_readiness_resource,
    "garmin://health/today": project_health_resource,
}


def project_surface_data(
    surface: str,
    data: Any,
    context: Mapping[str, object] | None = None,
) -> Any:
    """Apply the mandatory projector for a registered surface."""
    try:
        projector = PROJECTORS[surface]
    except KeyError as exc:
        raise ValueError(f"No response projector registered for surface '{surface}'") from exc
    return projector(data, context)


__all__ = [
    "EXPOSURE_POLICIES",
    "PROJECTORS",
    "SurfaceExposurePolicy",
    "project_surface_data",
]
