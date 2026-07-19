"""Executable public-data exposure contract for every MCP surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SurfaceKind = Literal["tool", "resource"]


@dataclass(frozen=True)
class SurfaceExposurePolicy:
    """Public-data contract for one registered MCP surface."""

    kind: SurfaceKind
    projector: str
    data_classes: frozenset[str]
    operational_identifiers: frozenset[str] = frozenset()
    opt_ins: frozenset[str] = frozenset()
    external_content: bool = False
    raw_passthrough_reason: str | None = None


def _tool(
    projector: str,
    *data_classes: str,
    identifiers: tuple[str, ...] = (),
    opt_ins: tuple[str, ...] = (),
    external_content: bool = False,
) -> SurfaceExposurePolicy:
    return SurfaceExposurePolicy(
        "tool",
        projector,
        frozenset(data_classes),
        operational_identifiers=frozenset(identifiers),
        opt_ins=frozenset(opt_ins),
        external_content=external_content,
    )


def _resource(projector: str, *data_classes: str) -> SurfaceExposurePolicy:
    return SurfaceExposurePolicy("resource", projector, frozenset(data_classes))


EXPOSURE_POLICIES: dict[str, SurfaceExposurePolicy] = {
    "query_activities": _tool(
        "activity_query", "activity", identifiers=("activityId",), opt_ins=("include_location",)
    ),
    "get_activity_details": _tool(
        "activity_details",
        "activity",
        "weather",
        "sensor_metrics",
        identifiers=("activityId", "gearPk", "uuid"),
        opt_ins=("include_location",),
    ),
    "get_activity_social": _tool(
        "social", "social_content", identifiers=("activityId",), external_content=True
    ),
    "compare_activities": _tool("comparison", "activity", identifiers=("activityId",)),
    "find_similar_activities": _tool("similar", "activity", identifiers=("activityId",)),
    "query_health_summary": _tool("health_summary", "health", "training"),
    "query_sleep_data": _tool("sleep", "sleep", "health"),
    "query_heart_rate_data": _tool("heart_rate", "heart_rate", "health"),
    "query_activity_metrics": _tool("activity_metrics", "health", "activity_metrics"),
    "query_devices": _tool("devices", "device", identifiers=("deviceId",)),
    "query_gear": _tool("gear", "gear", identifiers=("gearPk", "uuid")),
    "get_user_profile": _tool(
        "profile", "profile", "health", "device", identifiers=("activityId", "deviceId")
    ),
    "query_goals_and_records": _tool(
        "goals_records", "goals", "records", identifiers=("activityId",)
    ),
    "query_challenges": _tool("challenges", "challenges"),
    "analyze_training_period": _tool("training_period", "training", "activity"),
    "get_performance_metrics": _tool("performance", "training", "health"),
    "get_training_effect": _tool(
        "training_effect", "training", "activity", identifiers=("activityId",)
    ),
    "query_weight_data": _tool("weight", "weight", "body_composition"),
    "add_weight_entry": _tool("write", "weight", "mutation_result"),
    "delete_weight_entries": _tool("write", "weight", "mutation_result"),
    "query_workouts": _tool("workouts", "workout", identifiers=("workoutId",)),
    "upload_workout": _tool("write", "workout", "mutation_result"),
    "log_body_composition": _tool("write", "body_composition", "mutation_result"),
    "log_blood_pressure": _tool("write", "blood_pressure", "mutation_result"),
    "log_hydration": _tool("write", "hydration", "mutation_result"),
    "query_womens_health": _tool("womens_health", "reproductive_health"),
    "garmin://athlete/profile": _resource("athlete_resource", "profile", "health"),
    "garmin://training/readiness": _resource("health_resource", "training", "health"),
    "garmin://health/today": _resource("health_resource", "health"),
}
