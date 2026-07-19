"""Training and performance tools for Garmin Connect MCP server."""

from collections import defaultdict
from datetime import date as calendar_date
from datetime import timedelta
from typing import Annotated, Any

from fastmcp import Context

from ..client import GarminAPIError
from ..query_budget import (
    ApiCallBudgetExceededError,
    InvalidDateRangeError,
    QueryBudgetError,
    current_request_budget,
    policy_for_surface,
    reserve_projected_response_items,
    validate_date_range,
)
from ..response_builder import ResponseBuilder
from ..time_utils import (
    format_date_for_api,
    get_range_description,
    get_today_date_string,
    get_week_ranges,
    parse_time_range,
)
from ..types import UnitSystem


def _activity_calendar_date(activity: dict[str, Any]) -> str | None:
    """Return a validated calendar date without ending a scan on bad upstream data."""
    timestamp = activity.get("startTimeLocal") or activity.get("startTimeGMT")
    candidate = str(timestamp)[:10] if timestamp is not None else ""
    try:
        return calendar_date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


async def analyze_training_period(
    period: Annotated[
        str, "Time period: '7d', '30d', '90d', 'ytd', 'this-month', or 'YYYY-MM-DD:YYYY-MM-DD'"
    ] = "30d",
    activity_type: Annotated[
        str, "Filter by activity type (e.g., 'running', 'cycling'). Empty for all."
    ] = "",
    unit: Annotated[UnitSystem, "Unit system: 'metric' or 'imperial'"] = "metric",
    ctx: Context | None = None,
) -> str:
    """
    Analyze training over a specified period with comprehensive insights.

    Provides:
    - Total volume (activities, distance, time, elevation)
    - Activity type breakdown
    - Weekly trends
    - Performance insights

    Example periods: "30d", "this-month", "2024-01-01:2024-01-31"
    """
    assert ctx is not None
    try:
        # Parse period
        try:
            start_date, end_date = parse_time_range(period)
        except (OverflowError, ValueError) as exc:
            raise InvalidDateRangeError from exc
        bounded = validate_date_range(
            start_date,
            end_date,
            policy=policy_for_surface("analyze_training_period"),
        )
        period_description = get_range_description(period)

        # Page the upstream collection so neither source items nor calls are unbounded.
        start_str = bounded.start_iso
        end_str = bounded.end_iso
        budget = current_request_budget()
        if budget is not None:
            budget.require_calls(1)
        client = await ctx.get_state("client")
        activities: list[dict[str, Any]] = []
        offset = 0
        upstream_page_size = 50
        exhausted = False
        while not exhausted:
            if budget is not None and budget.calls_used >= budget.policy.max_api_calls:
                raise ApiCallBudgetExceededError
            batch = await client.call(
                "get_activities",
                offset,
                upstream_page_size,
                activity_type or None,
            )
            if not isinstance(batch, list) or not batch:
                break
            for activity in batch:
                if not isinstance(activity, dict):
                    continue
                activity_date = _activity_calendar_date(activity)
                if activity_date is None:
                    continue
                if activity_date > end_str:
                    continue
                if activity_date < start_str:
                    # Do not end a recency scan on local calendar time: travel can
                    # make a later feed row re-enter the requested local date range.
                    continue
                activities.append(activity)
            offset += len(batch)
            if len(batch) < upstream_page_size:
                exhausted = True

        if not activities or len(activities) == 0:
            return ResponseBuilder.build_response(
                data={
                    "period": {
                        "description": period_description,
                        "start_date": start_str,
                        "end_date": end_str,
                        "days": (end_date - start_date).days + 1,
                    },
                    "summary": {
                        "total_activities": 0,
                    },
                },
                analysis={"insights": ["No activities found in this period"]},
                metadata={"period": period, "activity_type": activity_type or "all"},
                surface="analyze_training_period",
            )

        # Calculate summary metrics
        total_distance = sum(act.get("distance", 0) or 0 for act in activities)
        total_time = sum(act.get("duration", 0) or 0 for act in activities)
        total_elevation = sum(act.get("elevationGain", 0) or 0 for act in activities)

        # Group by activity type
        by_type: dict[str, Any] = defaultdict(lambda: {"count": 0, "distance": 0, "time": 0})
        for act in activities:
            act_type = act.get("activityType", {}).get("typeKey", "unknown")
            by_type[act_type]["count"] += 1
            by_type[act_type]["distance"] += act.get("distance", 0) or 0
            by_type[act_type]["time"] += act.get("duration", 0) or 0

        # Format by_type for output
        by_type_list = []
        for type_key, data in sorted(by_type.items(), key=lambda x: x[1]["count"], reverse=True):
            percentage = (data["count"] / len(activities) * 100) if activities else 0
            by_type_list.append(
                {
                    "type": type_key,
                    "count": data["count"],
                    "percentage": round(percentage, 1),
                    "distance": {
                        "meters": data["distance"],
                        "formatted": ResponseBuilder._format_distance(data["distance"], unit),
                    },
                    "time": {
                        "seconds": data["time"],
                        "formatted": ResponseBuilder._format_duration(data["time"]),
                    },
                }
            )

        # Weekly breakdown
        weeks = get_week_ranges(start_date, end_date)
        date_to_week: dict[str, int] = {}
        for index, (week_start, week_end) in enumerate(weeks):
            current = week_start
            while current <= week_end:
                date_to_week[format_date_for_api(current)] = index
                current += timedelta(days=1)
        weekly_totals = [{"activities": 0, "distance": 0.0, "time": 0.0} for _week in weeks]
        for activity in activities:
            activity_date = _activity_calendar_date(activity)
            week_index = date_to_week.get(activity_date) if activity_date is not None else None
            if week_index is None:
                continue
            totals = weekly_totals[week_index]
            totals["activities"] += 1
            totals["distance"] += float(activity.get("distance", 0) or 0)
            totals["time"] += float(activity.get("duration", 0) or 0)

        weekly_trends = []
        for (week_start, week_end), totals in zip(weeks, weekly_totals, strict=True):
            weekly_trends.append(
                {
                    "week_start": ResponseBuilder.format_date_with_day(week_start),
                    "week_end": ResponseBuilder.format_date_with_day(week_end),
                    "activities": int(totals["activities"]),
                    "distance": {
                        "meters": totals["distance"],
                        "formatted": ResponseBuilder._format_distance(
                            float(totals["distance"]), unit
                        ),
                    },
                    "time": {
                        "seconds": totals["time"],
                        "formatted": ResponseBuilder._format_duration(float(totals["time"])),
                    },
                }
            )
        if budget is not None:
            budget.reserve_items(len(by_type_list) + len(weekly_trends))

        # Build data structure
        days_in_period = bounded.day_count
        data = {
            "period": {
                "description": period_description,
                "start_date": start_str,
                "end_date": end_str,
                "days": days_in_period,
            },
            "summary": {
                "total_activities": len(activities),
                "total_distance": {
                    "meters": total_distance,
                    "formatted": ResponseBuilder._format_distance(total_distance, unit),
                },
                "total_time": {
                    "seconds": total_time,
                    "formatted": ResponseBuilder._format_duration(total_time),
                },
                "total_elevation": {
                    "meters": total_elevation,
                    "formatted": ResponseBuilder._format_elevation(total_elevation, unit),
                },
                "averages": {
                    "distance_per_activity": {
                        "meters": total_distance / len(activities) if activities else 0,
                        "formatted": ResponseBuilder._format_distance(
                            total_distance / len(activities) if activities else 0, unit
                        ),
                    },
                    "activities_per_week": round(len(activities) / (days_in_period / 7), 1)
                    if days_in_period > 0
                    else 0,
                },
            },
            "by_activity_type": by_type_list,
            "trends": {"weekly": weekly_trends},
        }

        # Generate insights
        insights = []

        # Activity volume insight
        if len(activities) >= 15:
            insights.append(
                f"High training volume: {len(activities)} activities in {days_in_period} days"
            )
        elif len(activities) >= 8:
            insights.append(
                f"Moderate training volume: {len(activities)} activities in {days_in_period} days"
            )
        else:
            insights.append(
                f"Light training volume: {len(activities)} activities in {days_in_period} days"
            )

        # Trend insight
        if len(weekly_trends) >= 2:
            first_half = weekly_trends[: len(weekly_trends) // 2]
            second_half = weekly_trends[len(weekly_trends) // 2 :]
            first_half_count = sum(w["activities"] for w in first_half)
            second_half_count = sum(w["activities"] for w in second_half)

            if second_half_count > first_half_count * 1.2:
                insights.append("Training volume increasing over time")
            elif second_half_count < first_half_count * 0.8:
                insights.append("Training volume decreasing over time")
            else:
                insights.append("Training volume relatively consistent")

        # Activity type insight
        if by_type_list:
            dominant_type = by_type_list[0]
            if dominant_type["percentage"] > 70:
                insights.append(f"Training heavily focused on {dominant_type['type']}")
            elif dominant_type["percentage"] > 50:
                insights.append(f"Training primarily focused on {dominant_type['type']}")
            else:
                insights.append("Varied training across multiple activity types")

        return ResponseBuilder.build_response(
            data=data,
            analysis={"insights": insights},
            metadata={"period": period, "activity_type": activity_type or "all", "unit": unit},
            surface="analyze_training_period",
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def get_performance_metrics(
    date: Annotated[str | None, "Specific date (YYYY-MM-DD) for single-day metrics"] = None,
    start_date: Annotated[str | None, "Start date (YYYY-MM-DD) for range metrics"] = None,
    end_date: Annotated[str | None, "End date (YYYY-MM-DD) for range metrics"] = None,
    include_vo2_max: Annotated[bool, "Include VO2 max data"] = True,
    include_hill_score: Annotated[bool, "Include hill climbing score"] = True,
    include_endurance_score: Annotated[bool, "Include endurance score"] = True,
    include_hrv: Annotated[bool, "Include heart rate variability"] = True,
    include_fitness_age: Annotated[bool, "Include fitness age calculation"] = True,
    ctx: Context | None = None,
) -> str:
    """
    Get comprehensive performance metrics.

    Includes VO2 max, hill score, endurance score, heart rate variability,
    and fitness age data.

    Supports both single-date and date-range queries.
    """
    assert ctx is not None
    try:
        # Determine query type
        if date and (start_date is not None or end_date is not None):
            return ResponseBuilder.build_error_response(
                "date cannot be combined with start_date or end_date",
                "invalid_parameters",
            )
        if (start_date is None) != (end_date is None):
            from ..query_budget import InvalidDateRangeError

            raise InvalidDateRangeError
        policy = policy_for_surface("get_performance_metrics")
        if date:
            # Single date query
            is_range = False
            query_date = validate_date_range(date, date, policy=policy).start_iso
        elif start_date is not None and end_date is not None:
            # Range query
            is_range = True
            bounded = validate_date_range(start_date, end_date, policy=policy)
            query_start = bounded.start_iso
            query_end = bounded.end_iso
        else:
            # Default to today
            is_range = False
            query_date = get_today_date_string()

        required_calls = (
            int(include_hill_score) + int(include_endurance_score)
            if is_range
            else int(include_vo2_max) + int(include_hrv) + int(include_fitness_age)
        )
        budget = current_request_budget()
        if budget is not None:
            budget.require_calls(required_calls)
        client = await ctx.get_state("client")
        metrics_data: dict[str, Any] = {}

        # Single-date metrics
        if not is_range:
            # VO2 max (max metrics)
            if include_vo2_max:
                try:
                    vo2_max = await client.call("get_max_metrics", query_date)
                    metrics_data["vo2_max"] = vo2_max
                except QueryBudgetError:
                    raise
                except Exception:
                    metrics_data["vo2_max"] = None

            # HRV
            if include_hrv:
                try:
                    hrv = await client.call("get_hrv_data", query_date)
                    metrics_data["hrv"] = hrv
                except QueryBudgetError:
                    raise
                except Exception:
                    metrics_data["hrv"] = None

            # Fitness age
            if include_fitness_age:
                try:
                    fitness_age = await client.call("get_fitnessage_data", query_date)
                    metrics_data["fitness_age"] = fitness_age
                except QueryBudgetError:
                    raise
                except Exception:
                    metrics_data["fitness_age"] = None

            metadata = {"date": query_date}
        else:
            # Range metrics
            # Hill score
            if include_hill_score:
                try:
                    hill_score = await client.call("get_hill_score", query_start, query_end)
                    metrics_data["hill_score"] = hill_score
                except QueryBudgetError:
                    raise
                except Exception:
                    metrics_data["hill_score"] = None

            # Endurance score
            if include_endurance_score:
                try:
                    endurance_score = await client.call(
                        "get_endurance_score", query_start, query_end
                    )
                    metrics_data["endurance_score"] = endurance_score
                except QueryBudgetError:
                    raise
                except Exception:
                    metrics_data["endurance_score"] = None

            metadata = {"start_date": query_start, "end_date": query_end}

        reserve_projected_response_items("get_performance_metrics", metrics_data)

        # Generate insights
        insights = []
        available_metrics = [k for k, v in metrics_data.items() if v is not None]
        if available_metrics:
            insights.append(f"Available performance metrics: {', '.join(available_metrics)}")
        else:
            insights.append("No performance metrics available for this period")

        return ResponseBuilder.build_response(
            data=metrics_data,
            analysis={"insights": insights} if insights else None,
            metadata=metadata,
            surface="get_performance_metrics",
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def get_training_effect(
    activity_id: Annotated[int | None, "Activity ID for training effect"] = None,
    start_date: Annotated[str | None, "Start date (YYYY-MM-DD) for progress summary"] = None,
    end_date: Annotated[str | None, "End date (YYYY-MM-DD) for progress summary"] = None,
    metric: Annotated[str, "Metric to track for progress summary"] = "distance",
    ctx: Context | None = None,
) -> str:
    """
    Get training effect and progress summary.

    Supports:
    1. Training effect for specific activity (provide activity_id)
    2. Progress summary over date range (provide start_date, end_date, metric)
    """
    assert ctx is not None
    try:
        if activity_id is not None and (start_date is not None or end_date is not None):
            return ResponseBuilder.build_error_response(
                "activity_id cannot be combined with a date range",
                "invalid_parameters",
            )
        if (start_date is None) != (end_date is None):
            from ..query_budget import InvalidDateRangeError

            raise InvalidDateRangeError
        bounded = (
            validate_date_range(
                start_date,
                end_date,
                policy=policy_for_surface("get_training_effect"),
            )
            if start_date is not None and end_date is not None
            else None
        )
        if activity_id is not None and activity_id <= 0:
            return ResponseBuilder.build_error_response(
                "activity_id must be positive", "invalid_parameters"
            )
        if activity_id is None and bounded is None:
            return ResponseBuilder.build_error_response(
                "Must provide either activity_id OR (start_date + end_date)",
                "invalid_parameters",
                [
                    "For training effect: provide activity_id",
                    "For progress summary: provide start_date, end_date, and optionally metric",
                ],
            )
        budget = current_request_budget()
        if budget is not None and (activity_id is not None or bounded is not None):
            budget.require_calls(1)
        client = await ctx.get_state("client")

        # Pattern 1: Training effect for activity
        if activity_id is not None:
            activity = await client.call("get_activity", activity_id)
            effect = {
                key: value
                for key, value in activity.items()
                if "trainingeffect" in key.lower() or key == "activityTrainingLoad"
            }
            data = {"training_effect": effect}
            reserve_projected_response_items("get_training_effect", data)

            return ResponseBuilder.build_response(
                data=data,
                analysis={
                    "insights": [
                        "Training-effect fields are sourced from the supported activity summary"
                    ]
                },
                metadata={"activity_id": activity_id, "source_method": "get_activity"},
                surface="get_training_effect",
            )

        # Pattern 2: Progress summary
        elif bounded is not None:
            summary = await client.call(
                "get_progress_summary_between_dates",
                bounded.start_iso,
                bounded.end_iso,
                metric,
            )
            data = {"progress_summary": summary}
            reserve_projected_response_items("get_training_effect", data)

            return ResponseBuilder.build_response(
                data=data,
                metadata={
                    "start_date": bounded.start_iso,
                    "end_date": bounded.end_iso,
                    "metric": metric,
                },
                surface="get_training_effect",
            )

        raise AssertionError("Validated training-effect mode was not dispatched")

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)
