# garminconnect compatibility

## Version policy

The supported runtime is exactly `garminconnect==0.3.6`. This is a tested contract, not a minimum
version. A dependency upgrade must update `pyproject.toml`, `uv.lock`, the executable matrix in
`src/garmin_connect_mcp/compatibility.py`, this document, and the contract tests in the same change.
The complete QA suite must pass without Garmin credentials or network access before the new version
is supported.

The executable matrix stores, for every surface below, representative positional and keyword
arguments that must bind to the real dependency signature plus the declared return shape. CI imports
the installed `Garmin` class, binds every call with `inspect.signature`, checks the evaluated return
annotation, verifies the installed version, and compares the matrix with the registered FastMCP
tools and resources.

## Surface matrix

| MCP surface | Kind | garminconnect 0.3.6 methods | Status |
| --- | --- | --- | --- |
| `query_activities` | Tool | `get_activities_by_date`, `get_activities`, `get_activity`, `get_last_activity` | Supported |
| `get_activity_details` | Tool | `get_activity`, `get_activity_splits`, `get_activity_details`, `get_activity_weather`, `get_activity_hr_in_timezones`, `get_activity_gear`, `get_activity_exercise_sets` | Supported |
| `get_activity_social` | Tool | None | Unavailable; returns `capability_unavailable` |
| `compare_activities` | Tool | `get_activity` | Supported |
| `find_similar_activities` | Tool | `get_activity`, `get_activities` | Supported |
| `query_health_summary` | Tool | `get_stats`, `get_user_summary`, `get_training_readiness`, `get_training_status`, `get_body_battery`, `get_body_battery_events` | Supported |
| `query_sleep_data` | Tool | `get_sleep_data` | Supported |
| `query_heart_rate_data` | Tool | `get_heart_rates`, `get_rhr_day` | Supported |
| `query_activity_metrics` | Tool | `get_steps_data`, `get_stress_data`, `get_respiration_data`, `get_spo2_data`, `get_floors`, `get_hydration_data`, `get_blood_pressure`, `get_body_composition` | Supported |
| `query_devices` | Tool | `get_devices`, `get_device_last_used`, `get_primary_training_device`, `get_device_settings`, `get_device_solar_data`, `get_device_alarms` | Supported |
| `query_gear` | Tool | `get_gear`, `get_gear_defaults`, `get_gear_stats` | Supported with explicit profile number and gear UUID for stats |
| `get_user_profile` | Tool | `get_full_name`, `get_user_profile`, `get_stats`, `get_user_summary`, `get_personal_record`, `get_devices`, `get_primary_training_device` | Supported |
| `query_goals_and_records` | Tool | `get_goals`, `get_personal_record`, `get_race_predictions` | Supported |
| `query_challenges` | Tool | `get_available_badge_challenges`, `get_non_completed_badge_challenges`, `get_earned_badges`, `get_badge_challenges`, `get_adhoc_challenges`, `get_inprogress_virtual_challenges` | Supported with bounded page arguments |
| `analyze_training_period` | Tool | `get_activities_by_date` | Supported |
| `get_performance_metrics` | Tool | `get_max_metrics`, `get_hrv_data`, `get_fitnessage_data`, `get_hill_score`, `get_endurance_score` | Supported |
| `get_training_effect` | Tool | `get_activity`, `get_progress_summary_between_dates` | Supported; activity effect fields come from the activity summary |
| `query_weight_data` | Tool | `get_daily_weigh_ins`, `get_weigh_ins` | Supported |
| `add_weight_entry` | Tool | `add_weigh_in(weight, "kg", local_timestamp)` | Supported, default-off write |
| `delete_weight_entries` | Tool | `delete_weigh_ins(date, true)` | Supported, default-off destructive write |
| `query_workouts` | Tool | `get_workouts`, `get_workout_by_id`, `download_workout` | Supported |
| `upload_workout` | Tool | `upload_workout` | Supported, default-off write |
| `log_body_composition` | Tool | `add_body_composition(timestamp=..., weight=..., percent_fat=..., percent_hydration=...)` | Supported, default-off write |
| `log_blood_pressure` | Tool | `set_blood_pressure(systolic, diastolic, pulse, timestamp)` | Supported, default-off write |
| `log_hydration` | Tool | `add_hydration_data(volume_ml, timestamp=..., cdate=...)` | Supported, default-off write |
| `query_womens_health` | Tool | `get_pregnancy_summary`, `get_menstrual_data_for_date`, `get_menstrual_calendar_data` | Supported |
| `garmin://athlete/profile` | Resource | `get_full_name`, `get_unit_system`, `get_user_summary(date)`, `get_stats(date)` | Supported |
| `garmin://training/readiness` | Resource | `get_training_readiness(date)` | Supported |
| `garmin://health/today` | Resource | `get_stats(date)` | Supported |

The exact call samples and return shapes (`object`, `array`, `string`, `number`, `null`, or `binary`)
live in the executable matrix so documentation cannot substitute for CI enforcement.

## Public response shape

The dependency return shape above describes what the adapter must be able to
consume, not what the MCP host receives. Every registered surface has a second
executable contract in `response_policy.contract.EXPOSURE_POLICIES` and a named
fail-closed projector. Unknown dependency fields are discarded at that
boundary. Contract tests require the compatibility matrix, FastMCP
registration, exposure registry, and projector registry to contain the same 26
tools and 3 resources.

Public responses use schema `2`. See [MCP response data exposure](data-exposure.md)
for the per-surface data classes, retained operational identifiers, exact
location opt-in, stable error codes, and schema-1 migration notes.

## Date and serialization rules

- Date-only API methods receive concrete `YYYY-MM-DD` strings. `today` and `yesterday` are resolved
  in the server process's local timezone before the dependency call.
- Date-only writes use local noon on the requested calendar date. The dependency derives UTC from
  that local timestamp; noon avoids midnight and daylight-saving boundary ambiguity.
- `download_workout` returns FIT bytes. `query_workouts(action="download")` exposes them as Base64
  with `application/vnd.garmin.fit`, byte length, and SHA-256 metadata, so the MCP response remains
  JSON serializable.
- `get_activity_social` remains registered for a stable public surface, but does not dispatch a
  nonexistent method. It returns the stable `capability_unavailable` error type for version 0.3.6.
- `query_activities` and `get_activity_details` expose exact location only when
  their MCP argument `include_location=true`; this response policy does not
  alter the dependency method signature.
- Binary FIT download content remains an explicitly requested workout result.
  Only its Base64 content, media type, encoding, size, and SHA-256 digest cross
  the public response boundary.
