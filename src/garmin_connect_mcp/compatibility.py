"""Executable Garmin API compatibility contract for the public MCP surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SUPPORTED_GARMINCONNECT_VERSION = "0.3.6"

ReturnShape = Literal["array", "binary", "null", "number", "object", "string"]
SurfaceKind = Literal["resource", "tool"]


@dataclass(frozen=True)
class GarminCallContract:
    """One dependency call shape used by an MCP surface."""

    method: str
    args: tuple[Any, ...] = ()
    kwargs: tuple[tuple[str, Any], ...] = ()
    return_shapes: frozenset[ReturnShape] = field(default_factory=frozenset)

    @property
    def keyword_arguments(self) -> dict[str, Any]:
        return dict(self.kwargs)


@dataclass(frozen=True)
class SurfaceContract:
    """Compatibility status and dependency calls for one registered surface."""

    kind: SurfaceKind
    calls: tuple[GarminCallContract, ...]
    supported: bool = True
    unavailable_reason: str | None = None


def _call(
    method: str,
    *args: Any,
    returns: tuple[ReturnShape, ...],
    **kwargs: Any,
) -> GarminCallContract:
    return GarminCallContract(
        method=method,
        args=args,
        kwargs=tuple(kwargs.items()),
        return_shapes=frozenset(returns),
    )


OBJECT_RETURNS = ("object",)
ARRAY_RETURNS = ("array",)
OBJECT_OR_ARRAY_RETURNS = ("object", "array")
OBJECT_OR_NULL_RETURNS = ("object", "null")
STRING_OR_NULL_RETURNS = ("string", "null")

# Compact aliases keep the surface matrix readable; the long names above define them.
O = OBJECT_RETURNS  # noqa: E741
A = ARRAY_RETURNS
OA = OBJECT_OR_ARRAY_RETURNS
ON = OBJECT_OR_NULL_RETURNS
SN = STRING_OR_NULL_RETURNS

# Samples deliberately use concrete ISO dates. Relative labels such as ``today`` are
# resolved by this project before the dependency boundary.
COMPATIBILITY_MATRIX: dict[str, SurfaceContract] = {
    "query_activities": SurfaceContract(
        "tool",
        (
            _call("get_activities_by_date", "2026-07-01", "2026-07-19", None, returns=A),
            _call("get_activities", 0, 20, None, returns=OA),
            _call("get_activity", 123, returns=O),
            _call("get_last_activity", returns=ON),
        ),
    ),
    "get_activity_details": SurfaceContract(
        "tool",
        (
            _call("get_activity", 123, returns=O),
            _call("get_activity_splits", 123, returns=O),
            _call("get_activity_details", 123, returns=O, maxchart=2000),
            _call("get_activity_weather", 123, returns=O),
            _call("get_activity_hr_in_timezones", 123, returns=O),
            _call("get_activity_gear", 123, returns=O),
            _call("get_activity_exercise_sets", 123, returns=O),
        ),
    ),
    "get_activity_social": SurfaceContract(
        "tool",
        (),
        supported=False,
        unavailable_reason=(
            "garminconnect 0.3.6 does not expose an activity social-interactions method"
        ),
    ),
    "compare_activities": SurfaceContract("tool", (_call("get_activity", 123, returns=O),)),
    "find_similar_activities": SurfaceContract(
        "tool",
        (
            _call("get_activity", 123, returns=O),
            _call("get_activities", 0, 100, None, returns=OA),
        ),
    ),
    "query_health_summary": SurfaceContract(
        "tool",
        (
            _call("get_stats", "2026-07-19", returns=O),
            _call("get_user_summary", "2026-07-19", returns=O),
            _call("get_training_readiness", "2026-07-19", returns=A),
            _call("get_training_status", "2026-07-19", returns=O),
            _call("get_body_battery", "2026-07-19", "2026-07-19", returns=A),
            _call("get_body_battery_events", "2026-07-19", returns=A),
        ),
    ),
    "query_sleep_data": SurfaceContract(
        "tool", (_call("get_sleep_data", "2026-07-19", returns=O),)
    ),
    "query_heart_rate_data": SurfaceContract(
        "tool",
        (
            _call("get_heart_rates", "2026-07-19", returns=O),
            _call("get_rhr_day", "2026-07-19", returns=O),
        ),
    ),
    "query_activity_metrics": SurfaceContract(
        "tool",
        (
            _call("get_steps_data", "2026-07-19", returns=A),
            _call("get_stress_data", "2026-07-19", returns=O),
            _call("get_respiration_data", "2026-07-19", returns=O),
            _call("get_spo2_data", "2026-07-19", returns=O),
            _call("get_floors", "2026-07-19", returns=O),
            _call("get_hydration_data", "2026-07-19", returns=O),
            _call("get_blood_pressure", "2026-07-01", "2026-07-19", returns=O),
            _call("get_body_composition", "2026-07-01", "2026-07-19", returns=O),
        ),
    ),
    "query_devices": SurfaceContract(
        "tool",
        (
            _call("get_devices", returns=A),
            _call("get_device_last_used", returns=O),
            _call("get_primary_training_device", returns=O),
            _call("get_device_settings", 123, returns=O),
            _call("get_device_solar_data", 123, "2026-07-19", "2026-07-19", returns=A),
            _call("get_device_alarms", returns=A),
        ),
    ),
    "query_gear": SurfaceContract(
        "tool",
        (
            _call("get_gear", "123", returns=O),
            _call("get_gear_defaults", "123", returns=O),
            _call("get_gear_stats", "gear-uuid", returns=O),
        ),
    ),
    "get_user_profile": SurfaceContract(
        "tool",
        (
            _call("get_full_name", returns=SN),
            _call("get_user_profile", returns=O),
            _call("get_stats", "2026-07-19", returns=O),
            _call("get_user_summary", "2026-07-19", returns=O),
            _call("get_personal_record", returns=O),
            _call("get_devices", returns=A),
            _call("get_primary_training_device", returns=O),
        ),
    ),
    "query_goals_and_records": SurfaceContract(
        "tool",
        (
            _call("get_goals", returns=A),
            _call("get_personal_record", returns=O),
            _call("get_race_predictions", returns=O),
        ),
    ),
    "query_challenges": SurfaceContract(
        "tool",
        (
            _call("get_available_badge_challenges", 0, 100, returns=O),
            _call("get_non_completed_badge_challenges", 0, 100, returns=O),
            _call("get_earned_badges", returns=A),
            _call("get_badge_challenges", 0, 100, returns=O),
            _call("get_adhoc_challenges", 0, 100, returns=O),
            _call("get_inprogress_virtual_challenges", 1, 100, returns=O),
        ),
    ),
    "analyze_training_period": SurfaceContract(
        "tool",
        (_call("get_activities_by_date", "2026-07-01", "2026-07-19", None, returns=A),),
    ),
    "get_performance_metrics": SurfaceContract(
        "tool",
        (
            _call("get_max_metrics", "2026-07-19", returns=O),
            _call("get_hrv_data", "2026-07-19", returns=ON),
            _call("get_fitnessage_data", "2026-07-19", returns=O),
            _call("get_hill_score", "2026-07-01", "2026-07-19", returns=O),
            _call("get_endurance_score", "2026-07-01", "2026-07-19", returns=O),
        ),
    ),
    "get_training_effect": SurfaceContract(
        "tool",
        (
            _call("get_activity", 123, returns=O),
            _call(
                "get_progress_summary_between_dates",
                "2026-07-01",
                "2026-07-19",
                "distance",
                returns=O,
            ),
        ),
    ),
    "query_weight_data": SurfaceContract(
        "tool",
        (
            _call("get_daily_weigh_ins", "2026-07-19", returns=O),
            _call("get_weigh_ins", "2026-07-01", "2026-07-19", returns=O),
        ),
    ),
    "add_weight_entry": SurfaceContract(
        "tool",
        (_call("add_weigh_in", 75.0, "kg", "2026-07-19T12:00:00", returns=ON),),
    ),
    "delete_weight_entries": SurfaceContract(
        "tool", (_call("delete_weigh_ins", "2026-07-19", True, returns=("number", "null")),)
    ),
    "query_workouts": SurfaceContract(
        "tool",
        (
            _call("get_workouts", returns=A),
            _call("get_workout_by_id", 123, returns=O),
            _call("download_workout", 123, returns=("binary",)),
        ),
    ),
    "upload_workout": SurfaceContract(
        "tool", (_call("upload_workout", {"workoutName": "Tempo"}, returns=O),)
    ),
    "log_body_composition": SurfaceContract(
        "tool",
        (
            _call(
                "add_body_composition",
                returns=O,
                timestamp="2026-07-19T12:00:00",
                weight=75.0,
                percent_fat=15.0,
                percent_hydration=55.0,
            ),
        ),
    ),
    "log_blood_pressure": SurfaceContract(
        "tool",
        (_call("set_blood_pressure", 120, 80, 60, "2026-07-19T12:00:00", returns=O),),
    ),
    "log_hydration": SurfaceContract(
        "tool",
        (
            _call(
                "add_hydration_data",
                500.0,
                returns=O,
                timestamp="2026-07-19T12:00:00",
                cdate="2026-07-19",
            ),
        ),
    ),
    "query_womens_health": SurfaceContract(
        "tool",
        (
            _call("get_pregnancy_summary", returns=O),
            _call("get_menstrual_data_for_date", "2026-07-19", returns=O),
            _call("get_menstrual_calendar_data", "2026-07-01", "2026-07-19", returns=O),
        ),
    ),
    "garmin://athlete/profile": SurfaceContract(
        "resource",
        (
            _call("get_full_name", returns=SN),
            _call("get_unit_system", returns=SN),
            _call("get_user_summary", "2026-07-19", returns=O),
            _call("get_stats", "2026-07-19", returns=O),
        ),
    ),
    "garmin://training/readiness": SurfaceContract(
        "resource", (_call("get_training_readiness", "2026-07-19", returns=A),)
    ),
    "garmin://health/today": SurfaceContract(
        "resource", (_call("get_stats", "2026-07-19", returns=O),)
    ),
}


def dependency_methods() -> frozenset[str]:
    """Return every dependency method intentionally referenced by the MCP surface."""

    return frozenset(
        call.method for surface in COMPATIBILITY_MATRIX.values() for call in surface.calls
    )


def unavailable_message(surface_name: str) -> str:
    """Build the stable message for an intentionally unsupported capability."""

    contract = COMPATIBILITY_MATRIX[surface_name]
    if contract.supported or contract.unavailable_reason is None:
        raise ValueError(f"Surface '{surface_name}' is not marked unavailable")
    return (
        f"Capability '{surface_name}' is unavailable with garminconnect=="
        f"{SUPPORTED_GARMINCONNECT_VERSION}: {contract.unavailable_reason}."
    )
