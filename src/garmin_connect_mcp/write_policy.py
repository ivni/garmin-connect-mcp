"""Default-off Garmin mutation capabilities and least-privilege method policy."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .client import GarminMethodNotAllowedError, MutationOperation

WRITE_CAPABILITIES_ENV = "GARMIN_WRITE_CAPABILITIES"

READ_METHODS = frozenset(
    {
        "download_workout",
        "get_activities",
        "get_activities_by_date",
        "get_activity",
        "get_activity_details",
        "get_activity_exercise_sets",
        "get_activity_gear",
        "get_activity_hr_in_timezones",
        "get_activity_splits",
        "get_activity_weather",
        "get_adhoc_challenges",
        "get_available_badge_challenges",
        "get_badge_challenges",
        "get_blood_pressure",
        "get_body_battery",
        "get_body_battery_events",
        "get_body_composition",
        "get_daily_weigh_ins",
        "get_device_alarms",
        "get_device_last_used",
        "get_device_settings",
        "get_device_solar_data",
        "get_devices",
        "get_earned_badges",
        "get_endurance_score",
        "get_fitnessage_data",
        "get_floors",
        "get_full_name",
        "get_gear",
        "get_gear_defaults",
        "get_gear_stats",
        "get_goals",
        "get_heart_rates",
        "get_hill_score",
        "get_hrv_data",
        "get_hydration_data",
        "get_inprogress_virtual_challenges",
        "get_last_activity",
        "get_max_metrics",
        "get_menstrual_data_for_date",
        "get_menstrual_calendar_data",
        "get_non_completed_badge_challenges",
        "get_personal_record",
        "get_pregnancy_summary",
        "get_primary_training_device",
        "get_progress_summary_between_dates",
        "get_race_predictions",
        "get_respiration_data",
        "get_rhr_day",
        "get_sleep_data",
        "get_spo2_data",
        "get_stats",
        "get_steps_data",
        "get_stress_data",
        "get_training_readiness",
        "get_training_status",
        "get_unit_system",
        "get_user_profile",
        "get_user_summary",
        "get_weigh_ins",
        "get_workout_by_id",
        "get_workouts",
    }
)

MUTATION_TOOLS = {
    "add_weight_entry": MutationOperation(
        capability="weight.write",
        method_name="add_weigh_in",
        reconciliation=(
            "query weight data for the target date and compare the weight and timestamp; "
            "retry with a new key only when the entry is confirmed absent"
        ),
    ),
    "delete_weight_entries": MutationOperation(
        capability="weight.delete",
        method_name="delete_weigh_ins",
        reconciliation=(
            "query all weight data for the target date and verify whether any weigh-ins remain "
            "before attempting another date-wide deletion"
        ),
    ),
    "upload_workout": MutationOperation(
        capability="workouts.upload",
        method_name="upload_workout",
        reconciliation=(
            "list workouts and compare the uploaded workout's identifying fields before uploading "
            "again"
        ),
    ),
    "log_body_composition": MutationOperation(
        capability="health.body_composition",
        method_name="add_body_composition",
        reconciliation=(
            "query body-composition data for the target date and compare the submitted values "
            "before writing again"
        ),
    ),
    "log_blood_pressure": MutationOperation(
        capability="health.blood_pressure",
        method_name="set_blood_pressure",
        reconciliation=(
            "query blood-pressure data for the target date and compare the submitted values "
            "before writing again"
        ),
    ),
    "log_hydration": MutationOperation(
        capability="health.hydration",
        method_name="add_hydration_data",
        reconciliation=(
            "query hydration data for the target date and compare the daily total before writing "
            "again"
        ),
    ),
}

WRITE_CAPABILITIES = frozenset(operation.capability for operation in MUTATION_TOOLS.values())


class WritePolicy:
    """Immutable set of mutation capabilities enabled for this server process."""

    def __init__(self, enabled_capabilities: frozenset[str] = frozenset()):
        unknown = enabled_capabilities - WRITE_CAPABILITIES
        if unknown:
            values = ", ".join(sorted(unknown))
            raise ValueError(f"Unknown Garmin write capabilities: {values}")
        self.enabled_capabilities = enabled_capabilities

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> WritePolicy:
        values = os.environ if environ is None else environ
        raw = values.get(WRITE_CAPABILITIES_ENV, "")
        enabled = frozenset(part.strip() for part in raw.split(",") if part.strip())
        return cls(enabled)

    def operation_for_call(
        self,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> MutationOperation | None:
        """Return an authorized operation, or no operation for read/dry-run calls."""
        operation = MUTATION_TOOLS.get(tool_name)
        if operation is None or arguments.get("dry_run", True) is not False:
            return None
        if operation.capability not in self.enabled_capabilities:
            raise GarminMethodNotAllowedError(
                f"Garmin write capability '{operation.capability}' is disabled. "
                f"Set {WRITE_CAPABILITIES_ENV} to an explicit comma-separated capability list "
                "when starting the server."
            )
        return operation
