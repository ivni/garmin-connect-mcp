"""Purpose-bound public projections for health, wellness, and training metrics."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .common import project_date, project_list, project_object, project_tree

TIME_FIELDS = frozenset(
    {
        "calendarDate",
        "date",
        "timestamp",
        "startTimestampGMT",
        "endTimestampGMT",
        "startTimestampLocal",
        "endTimestampLocal",
        "wellnessStartTimeGmt",
        "wellnessStartTimeLocal",
        "wellnessEndTimeGmt",
        "wellnessEndTimeLocal",
        "startGMT",
        "endGMT",
        "datetime",
        "day_of_week",
        "formatted",
    }
)

DAILY_HEALTH_FIELDS = TIME_FIELDS | frozenset(
    {
        "totalKilocalories",
        "activeKilocalories",
        "bmrKilocalories",
        "wellnessKilocalories",
        "consumedKilocalories",
        "remainingKilocalories",
        "totalSteps",
        "steps",
        "stepGoal",
        "dailyStepGoal",
        "totalDistance",
        "totalDistanceMeters",
        "wellnessDistanceMeters",
        "distance",
        "durationInMilliseconds",
        "highlyActiveSeconds",
        "activeSeconds",
        "sedentarySeconds",
        "sleepingSeconds",
        "moderateIntensityMinutes",
        "vigorousIntensityMinutes",
        "intensityMinutesGoal",
        "floorsAscended",
        "floorsDescended",
        "floorsAscendedInMeters",
        "floorsDescendedInMeters",
        "minHeartRate",
        "maxHeartRate",
        "restingHeartRate",
        "lastSevenDaysAvgRestingHeartRate",
        "averageHeartRate",
        "averageStressLevel",
        "avgStressLevel",
        "maxStressLevel",
        "stressDuration",
        "restStressDuration",
        "activityStressDuration",
        "uncategorizedStressDuration",
        "lowStressDuration",
        "mediumStressDuration",
        "highStressDuration",
        "stressQualifier",
        "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue",
        "bodyBatteryHighestValue",
        "bodyBatteryLowestValue",
        "bodyBatteryMostRecentValue",
        "bodyBatteryDuringSleep",
        "bodyBatteryAtWakeTime",
        "bodyBatteryValuesArray",
        "bodyBatteryDynamicFeedbackEvent",
        "eventType",
        "eventStartTimeGmt",
        "eventEndTimeGmt",
        "bodyBatteryImpact",
        "averageSpo2",
        "lowestSpo2",
        "latestSpo2",
        "latestSpo2ReadingTimeGmt",
        "latestSpo2ReadingTimeLocal",
        "avgWakingRespirationValue",
        "highestRespirationValue",
        "lowestRespirationValue",
        "latestRespirationValue",
        "latestRespirationTimeGMT",
        "hydrationGoal",
        "valueInML",
        "sweatLossInML",
        "weight",
        "weightInGrams",
        "bmi",
        "bodyFat",
        "bodyWater",
        "boneMass",
        "muscleMass",
        "physiqueRating",
        "metabolicAge",
        "visceralFat",
        "value",
        "values",
        "descriptor",
        "key",
        "unit",
    }
)

READINESS_FIELDS = TIME_FIELDS | frozenset(
    {
        "trainingReadinessScore",
        "score",
        "level",
        "feedbackShort",
        "feedbackLong",
        "recoveryTime",
        "recoveryTimeMinutes",
        "sleepScore",
        "sleepHistoryFactorPercent",
        "hrvFactorPercent",
        "acuteLoadFactorPercent",
        "stressHistoryFactorPercent",
        "recoveryTimeFactorPercent",
        "bodyBatteryDuringSleep",
        "bodyBatteryAtWakeTime",
        "bodyBatteryMostRecentValue",
    }
)

TRAINING_STATUS_FIELDS = TIME_FIELDS | frozenset(
    {
        "trainingStatus",
        "trainingStatusFeedbackPhrase",
        "loadRatio",
        "acuteTrainingLoad",
        "chronicTrainingLoad",
        "loadFocus",
        "vo2Max",
        "vO2MaxValue",
        "status",
        "value",
        "unit",
    }
)

BODY_BATTERY_FIELDS = TIME_FIELDS | frozenset(
    {
        "bodyBatteryChargedValue",
        "bodyBatteryDrainedValue",
        "bodyBatteryHighestValue",
        "bodyBatteryLowestValue",
        "bodyBatteryMostRecentValue",
        "bodyBatteryDuringSleep",
        "bodyBatteryAtWakeTime",
        "bodyBatteryValuesArray",
        "bodyBatteryDynamicFeedbackEvent",
        "eventType",
        "eventStartTimeGmt",
        "eventEndTimeGmt",
        "bodyBatteryImpact",
        "value",
    }
)

SLEEP_FIELDS = TIME_FIELDS | frozenset(
    {
        "dailySleepDTO",
        "sleepTimeSeconds",
        "napTimeSeconds",
        "sleepStartTimestampGMT",
        "sleepStartTimestampLocal",
        "sleepEndTimestampGMT",
        "sleepEndTimestampLocal",
        "deepSleepSeconds",
        "lightSleepSeconds",
        "remSleepSeconds",
        "awakeSleepSeconds",
        "unmeasurableSleepSeconds",
        "averageSpO2Value",
        "lowestSpO2Value",
        "highestSpO2Value",
        "averageRespirationValue",
        "lowestRespirationValue",
        "highestRespirationValue",
        "avgSleepStress",
        "sleepScores",
        "overall",
        "quality",
        "recovery",
        "duration",
        "stress",
        "value",
        "qualifierKey",
        "awakeCount",
        "interruptions",
        "sleepNeed",
        "sleepCoach",
        "sleepScoreFeedback",
        "sleepScoreInsight",
        "bodyBatteryChange",
        "restingHeartRate",
        "avgOvernightHrv",
        "restlessMomentsCount",
        "sleepLevels",
        "sleepMovement",
        "sleepHeartRate",
        "wellnessEpochRespirationDataDTOList",
        "respirationValue",
        "activityLevel",
    }
)

HEART_RATE_FIELDS = TIME_FIELDS | frozenset(
    {
        "minHeartRate",
        "maxHeartRate",
        "restingHeartRate",
        "lastSevenDaysAvgRestingHeartRate",
        "averageHeartRate",
        "heartRateValues",
        "heartRateValueDescriptors",
        "avgOvernightHrv",
        "value",
        "values",
        "descriptor",
    }
)

STEPS_FIELDS = TIME_FIELDS | frozenset(
    {
        "totalSteps",
        "steps",
        "stepGoal",
        "dailyStepGoal",
        "totalDistance",
        "totalDistanceMeters",
        "activeKilocalories",
        "stepsArray",
        "value",
    }
)

STRESS_FIELDS = TIME_FIELDS | frozenset(
    {
        "averageStressLevel",
        "avgStressLevel",
        "maxStressLevel",
        "stressDuration",
        "restStressDuration",
        "activityStressDuration",
        "uncategorizedStressDuration",
        "lowStressDuration",
        "mediumStressDuration",
        "highStressDuration",
        "stressQualifier",
        "stressValuesArray",
        "bodyBatteryValuesArray",
        "value",
    }
)

RESPIRATION_FIELDS = TIME_FIELDS | frozenset(
    {
        "avgWakingRespirationValue",
        "highestRespirationValue",
        "lowestRespirationValue",
        "latestRespirationValue",
        "latestRespirationTimeGMT",
        "wellnessEpochRespirationDataDTOList",
        "respirationValue",
        "value",
    }
)

SPO2_FIELDS = TIME_FIELDS | frozenset(
    {
        "averageSpo2",
        "lowestSpo2",
        "latestSpo2",
        "latestSpo2ReadingTimeGmt",
        "latestSpo2ReadingTimeLocal",
        "averageSpO2Value",
        "lowestSpO2Value",
        "highestSpO2Value",
        "value",
    }
)

FLOORS_FIELDS = TIME_FIELDS | frozenset(
    {
        "floorsAscended",
        "floorsDescended",
        "floorsAscendedInMeters",
        "floorsDescendedInMeters",
        "value",
    }
)

HYDRATION_FIELDS = TIME_FIELDS | frozenset(
    {"hydrationGoal", "valueInML", "sweatLossInML", "value", "unit"}
)
BLOOD_PRESSURE_FIELDS = TIME_FIELDS | frozenset({"systolic", "diastolic", "pulse"})
BODY_COMPOSITION_FIELDS = TIME_FIELDS | frozenset(
    {
        "weight",
        "weightInGrams",
        "bmi",
        "bodyFat",
        "bodyWater",
        "boneMass",
        "muscleMass",
        "physiqueRating",
        "metabolicAge",
        "visceralFat",
    }
)

VO2_FIELDS = TIME_FIELDS | frozenset(
    {"vo2Max", "vO2MaxValue", "generic", "cycling", "running", "sport", "value", "unit"}
)
HRV_FIELDS = TIME_FIELDS | frozenset(
    {
        "hrvStatus",
        "weeklyAvg",
        "lastNightAvg",
        "avgOvernightHrv",
        "baselineLowUpper",
        "baselineBalancedLow",
        "baselineBalancedUpper",
        "baselineHighLower",
        "status",
        "value",
    }
)
FITNESS_AGE_FIELDS = TIME_FIELDS | frozenset(
    {"fitnessAge", "chronologicalAge", "achievableFitnessAge", "value"}
)
HILL_SCORE_FIELDS = TIME_FIELDS | frozenset(
    {"hillScore", "strengthScore", "enduranceScore", "status", "value"}
)
ENDURANCE_SCORE_FIELDS = TIME_FIELDS | frozenset(
    {"enduranceScore", "classification", "status", "value"}
)

TRAINING_EFFECT_FIELDS = TIME_FIELDS | frozenset(
    {
        "training_effect",
        "progress_summary",
        "activity_id",
        "aerobicTrainingEffect",
        "anaerobicTrainingEffect",
        "activityTrainingLoad",
        "trainingEffectLabel",
        "distance",
        "duration",
        "value",
        "unit",
    }
)

WEIGHT_FIELDS = (
    TIME_FIELDS
    | BODY_COMPOSITION_FIELDS
    | frozenset(
        {
            "dateWeightList",
            "totalAverage",
            "startDate",
            "endDate",
            "sourceType",
        }
    )
)

REPRODUCTIVE_FIELDS = frozenset(
    {
        "calendarDate",
        "date",
        "startDate",
        "endDate",
        "pregnancyStartDate",
        "dueDate",
        "currentWeek",
        "currentDay",
        "trimester",
        "cycleStartDate",
        "cycleLength",
        "periodLength",
        "dayInCycle",
        "phase",
        "predictedCycleStartDate",
        "predictedPeriodStartDate",
        "predictedPeriodEndDate",
        "fertileWindowStartDate",
        "fertileWindowEndDate",
        "ovulationDate",
        "symptoms",
        "notes",
        "value",
        "type",
        "intensity",
    }
)


def project_health_payload(value: Any) -> Any:
    """Project the broad daily-health category used by stats resources."""
    return project_tree(value, DAILY_HEALTH_FIELDS)


def project_sleep_payload(value: Any) -> Any:
    return project_tree(value, SLEEP_FIELDS)


def _project_summary(value: Any) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "date": project_date,
            "stats": project_health_payload,
            "user_summary": project_health_payload,
            "training_readiness": lambda item: project_tree(item, READINESS_FIELDS),
            "training_status": lambda item: project_tree(item, TRAINING_STATUS_FIELDS),
            "body_battery": lambda item: project_tree(item, BODY_BATTERY_FIELDS),
            "body_battery_events": lambda item: project_tree(item, BODY_BATTERY_FIELDS),
        },
    )


def project_health_summary(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    if isinstance(value, Mapping) and "summaries" in value:
        return project_object(
            value,
            scalar_fields=frozenset({"count"}),
            field_projectors={"summaries": lambda item: project_list(item, _project_summary)},
        )
    return _project_summary(value)


def _project_sleep_entry(value: Any) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={"date": project_date, "sleep": project_sleep_payload},
    )


def project_sleep_response(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    if isinstance(value, Mapping) and "sleep_data" in value:
        return project_object(
            value,
            scalar_fields=frozenset({"count"}),
            field_projectors={"sleep_data": lambda item: project_list(item, _project_sleep_entry)},
        )
    return _project_sleep_entry(value)


def _project_heart_rate_entry(value: Any) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "date": project_date,
            "heart_rate": lambda item: project_tree(item, HEART_RATE_FIELDS),
            "resting_hr": lambda item: project_tree(item, HEART_RATE_FIELDS),
        },
    )


def project_heart_rate_response(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    if isinstance(value, Mapping) and "heart_rate_data" in value:
        return project_object(
            value,
            scalar_fields=frozenset({"count"}),
            field_projectors={
                "heart_rate_data": lambda item: project_list(item, _project_heart_rate_entry)
            },
        )
    return _project_heart_rate_entry(value)


def _project_metrics_entry(value: Any) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "date": project_date,
            "steps": lambda item: project_tree(item, STEPS_FIELDS),
            "stress": lambda item: project_tree(item, STRESS_FIELDS),
            "respiration": lambda item: project_tree(item, RESPIRATION_FIELDS),
            "spo2": lambda item: project_tree(item, SPO2_FIELDS),
            "floors": lambda item: project_tree(item, FLOORS_FIELDS),
            "hydration": lambda item: project_tree(item, HYDRATION_FIELDS),
            "blood_pressure": lambda item: project_tree(item, BLOOD_PRESSURE_FIELDS),
            "body_composition": lambda item: project_tree(item, WEIGHT_FIELDS),
        },
    )


def project_activity_metrics(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    if isinstance(value, Mapping) and "metrics" in value:
        return project_object(
            value,
            scalar_fields=frozenset({"count"}),
            field_projectors={"metrics": lambda item: project_list(item, _project_metrics_entry)},
        )
    return _project_metrics_entry(value)


def project_performance(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "vo2_max": lambda item: project_tree(item, VO2_FIELDS),
            "hrv": lambda item: project_tree(item, HRV_FIELDS),
            "fitness_age": lambda item: project_tree(item, FITNESS_AGE_FIELDS),
            "hill_score": lambda item: project_tree(item, HILL_SCORE_FIELDS),
            "endurance_score": lambda item: project_tree(item, ENDURANCE_SCORE_FIELDS),
        },
    )


def _project_weight_payload(value: Any) -> Any:
    return project_tree(value, WEIGHT_FIELDS)


def project_weight(value: Any, _context: Mapping[str, object] | None = None) -> dict[str, Any]:
    return project_object(
        value,
        scalar_fields=frozenset({"count", "date"}),
        field_projectors={
            "weight": _project_weight_payload,
            "weights": _project_weight_payload,
            "weigh_ins": _project_weight_payload,
            "daily_weigh_ins": _project_weight_payload,
        },
    )


def project_womens_health(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    return project_object(
        value,
        scalar_fields=frozenset({"date"}),
        field_projectors={
            "pregnancy_summary": lambda item: project_tree(item, REPRODUCTIVE_FIELDS),
            "menstrual_data": lambda item: project_tree(item, REPRODUCTIVE_FIELDS),
            "menstrual_calendar": lambda item: project_tree(item, REPRODUCTIVE_FIELDS),
        },
    )


def project_health_resource(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={"health": project_health_payload},
    )


def project_readiness_resource(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    return project_object(
        value,
        field_projectors={
            "readiness": lambda item: project_tree(item, READINESS_FIELDS),
        },
    )
