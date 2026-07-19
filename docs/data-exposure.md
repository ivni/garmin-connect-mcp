# MCP response data exposure

## Policy

Every tool and resource response uses response schema `2` and an explicit,
fail-closed projector. Garmin fields that are not listed by that projector are
removed, including unknown nested fields. Calling a tool is treated as an
explicit request for that tool's documented data category; the server does not
add unrelated data merely because Garmin returned it.

This is a response-minimization boundary, not an end-to-end confidentiality
guarantee. The selected MCP host and model receive the projected response and
may retain it in chat history, logs, or cloud sessions according to their own
policies. Read-only access prevents Garmin mutations; it does not make health
or profile data non-sensitive.

There is no generic raw or legacy-response mode. Clients that depended on
unspecified Garmin fields must migrate to the documented schema.

## Surface matrix

The following table describes the public `data` object. Dates, pagination,
server-computed summaries, and formatted units remain available where the tool
already produces them.

| MCP surface | Returned data | Retained operational IDs | Per-call opt-in |
| --- | --- | --- | --- |
| `query_activities` | Activity summaries and optional aggregate metrics | `activityId` | `include_location` |
| `get_activity_details` | Activity summary, splits, weather, HR zones, gear, and exercise sets | `activityId`, gear `gearPk`/`uuid` in details | `include_location` |
| `get_activity_social` | Unavailable capability response; future comments are external untrusted content | `activityId` | None |
| `compare_activities` | Projected activities and computed comparison | `activityId` | None |
| `find_similar_activities` | Reference activity, projected matches, and computed differences | `activityId` | None |
| `query_health_summary` | Daily stats, summary, training readiness/status, and Body Battery | None | None |
| `query_sleep_data` | Sleep stages, scores, timing, respiration, SpO2, stress, HR, and recovery fields | None | None |
| `query_heart_rate_data` | Heart-rate samples, descriptors, and resting-HR values | None | None |
| `query_activity_metrics` | Requested steps, stress, respiration, SpO2, floors, hydration, blood pressure, or body-composition metrics | None | None |
| `query_devices` | Model/display name, software/status/battery/sync data, plus projected settings, solar data, or alarms when requested | `deviceId` | None |
| `query_gear` | Gear names, status, dates, limits, defaults, and distance statistics | `gearPk`, `uuid` | None |
| `get_user_profile` | Display name, locale/unit preferences, daily health summary, records, and projected devices | `activityId` in records, `deviceId` for device follow-up | None |
| `query_goals_and_records` | Goals, personal records, and race predictions | Referenced `activityId` | None |
| `query_challenges` | Badge and challenge name, description, state, progress, dates, points, and participant count | None | None |
| `analyze_training_period` | Period totals, activity-type and weekly summaries, and server-computed analysis | None | None |
| `get_performance_metrics` | VO2 max, HRV, fitness age, hill/endurance scores, and their documented component values | None | None |
| `get_training_effect` | Activity training effect/load and progress summary | `activityId` | None |
| `query_weight_data` | Weight, BMI, body fat/water, bone/muscle mass, and related measurements | None | None |
| `add_weight_entry` | Preview or bounded mutation acknowledgement | None | None |
| `delete_weight_entries` | Preview/confirmation fields or bounded mutation acknowledgement | None | None |
| `query_workouts` | Workout name, sport, duration/distance, steps/targets, or FIT download metadata and Base64 content | `workoutId` | None |
| `upload_workout` | Preview or bounded mutation acknowledgement | Returned `workoutId` when available | None |
| `log_body_composition` | Preview or bounded mutation acknowledgement | None | None |
| `log_blood_pressure` | Preview or bounded mutation acknowledgement | None | None |
| `log_hydration` | Preview or bounded mutation acknowledgement | None | None |
| `query_womens_health` | The explicitly selected pregnancy summary, menstrual day, or menstrual calendar fields | None | None |
| `garmin://athlete/profile` | Compact display/unit profile and projected daily health data | None | None |
| `garmin://training/readiness` | Training-readiness score/factors, recovery time, sleep score, and relevant Body Battery values | None | None |
| `garmin://health/today` | Projected daily health snapshot | None | None |

## Domain field rules

- Activity data includes names/types, local/GMT times, duration, distance,
  pace/speed, heart rate, power, cadence, calories, elevation, training effect,
  respiration, laps, and intensity minutes when Garmin supplies them.
- Exact start/end latitude and longitude, a point latitude/longitude, and a
  Garmin location name are omitted by default. On `query_activities` or
  `get_activity_details`, `include_location=true` adds only a documented
  `location` block and sets `metadata.precise_location=true` when precise
  location was requested.
- Device serial numbers, `unitId`, owner/profile IDs, and undocumented internal
  IDs are omitted. `deviceId` remains because later device queries require it.
- Profile settings are limited to display/name, measurement system, locale,
  time/date formats, and first day of week. The complete upstream settings
  dictionary is never returned.
- Pregnancy and menstrual data appears only after a direct
  `query_womens_health` call. It is not added to general profile or health
  resources, and no additional global switch is required.
- Mutation responses retain the requested preview values and a locally created
  acknowledgement plus bounded boolean/count fields, deduplication state, and
  an operational activity/workout ID when applicable. Arbitrary upstream text
  acknowledgements are not returned.
- Provider comments are represented as structured external content with origin
  and trust metadata. They are data, not instructions from this server.

## Response envelope and migration

Successful responses retain the existing top-level `data`, optional `analysis`,
`pagination`, and `metadata` keys. `metadata.response_schema` is now `"2"`.
Unspecified raw Garmin fields that could appear in schema `1` are intentionally
removed. Unknown future Garmin fields remain absent until their purpose and
privacy impact are reviewed and added to the appropriate projector.

Error responses use `error.code`, `error.type`, a stable public message, and,
for classified runtime failures, a correlation `request_id`. Raw exception
text, URLs, response bodies, local paths, credentials, and tokens are not
returned. Public codes are:

| Code | Meaning |
| --- | --- |
| `AUTH_REQUIRED` | Garmin authentication is missing or expired |
| `RATE_LIMITED` | Garmin rejected the request because of rate limiting |
| `NOT_FOUND` | The requested Garmin object does not exist |
| `VALIDATION_ERROR` | A bounded input requirement was not met |
| `CAPABILITY_UNAVAILABLE` | The capability is absent from the installed Garmin dependency or disabled by explicit server policy |
| `GARMIN_UPSTREAM_UNAVAILABLE` | Garmin could not complete the request |
| `INTERNAL_ERROR` | An unexpected server failure occurred |

All error responses also carry `metadata.response_schema="2"`.
