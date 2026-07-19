"""Activity-related tools for Garmin Connect MCP server."""

from datetime import date as calendar_date
from typing import Annotated, Any

from fastmcp import Context

from ..client import GarminAPIError
from ..compatibility import unavailable_message
from ..pagination import (
    MAX_CONTINUATION_POSITION,
    decode_continuation_cursor,
    encode_continuation_cursor,
    validate_continuation_headroom,
)
from ..query_budget import (
    InvalidContinuationCursorError,
    QueryBudgetError,
    current_request_budget,
    policy_for_surface,
    reserve_projected_response_items,
    validate_date_range,
    validate_page_size,
)
from ..response_builder import ResponseBuilder
from ..types import UnitSystem


def _activity_calendar_date(activity: dict[str, Any]) -> str | None:
    """Return a validated calendar date without trusting malformed upstream rows."""
    timestamp = activity.get("startTimeLocal") or activity.get("startTimeGMT")
    candidate = str(timestamp)[:10] if timestamp is not None else ""
    try:
        return calendar_date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


async def _query_activities_paginated(
    ctx: Context,
    start_date: str,
    end_date: str,
    activity_type: str,
    cursor: str | None,
    limit: str | int | None,
    unit: UnitSystem,
    include_location: bool,
) -> str:
    """Page the upstream offset API while applying a bounded date filter."""
    surface = "query_activities"
    policy = policy_for_surface(surface)
    bounded = validate_date_range(start_date, end_date, policy=policy)
    filters: dict[str, Any] = {
        "start_date": bounded.start_iso,
        "end_date": bounded.end_iso,
        "activity_type": activity_type,
        "unit": unit,
        "include_location": include_location,
    }
    if cursor is None:
        position = 0
        page_size = validate_page_size(limit, policy)
    else:
        try:
            position, page_size = decode_continuation_cursor(
                cursor,
                surface=surface,
                filters=filters,
                policy=policy,
            )
        except ValueError as exc:
            raise InvalidContinuationCursorError from exc
        if limit is not None and validate_page_size(limit, policy) != page_size:
            raise InvalidContinuationCursorError

    maximum_scan_advance = policy.max_api_calls * policy.max_page_items
    if position + maximum_scan_advance > MAX_CONTINUATION_POSITION:
        raise InvalidContinuationCursorError

    activities: list[dict[str, Any]] = []
    activity_positions: list[int] = []
    scan_position = position
    next_position: int | None = None
    truncation_reason: str | None = None
    exhausted = False
    response_limited = False
    upstream_page_size = policy.max_page_items
    budget = current_request_budget()
    client = await ctx.get_state("client")

    while len(activities) <= page_size and not exhausted:
        if budget is not None and budget.calls_used >= policy.max_api_calls:
            next_position = scan_position
            truncation_reason = "api_call_budget"
            break
        batch_start = scan_position
        batch = await client.call(
            "get_activities",
            batch_start,
            upstream_page_size,
            activity_type or None,
        )
        if not isinstance(batch, list) or not batch:
            exhausted = True
            break
        for index, activity in enumerate(batch):
            scan_position = batch_start + index + 1
            if not isinstance(activity, dict):
                continue
            activity_date = _activity_calendar_date(activity)
            if activity_date is None:
                continue
            if activity_date > bounded.end_iso:
                continue
            if activity_date < bounded.start_iso:
                # Local calendar dates are not guaranteed to be monotonic in a
                # recency-ordered feed (travel and offset changes can reorder them).
                continue
            activities.append(activity)
            activity_positions.append(scan_position)
            if budget is not None:
                preview_cursor = encode_continuation_cursor(
                    surface=surface,
                    position=scan_position,
                    page_size=page_size,
                    filters=filters,
                )
                preview_size = ResponseBuilder.preview_response_size(
                    data={
                        "activities": [
                            ResponseBuilder.format_activity(act, unit) for act in activities
                        ],
                        "aggregated": ResponseBuilder.aggregate_activities(
                            activities,
                            unit,
                        ),
                    },
                    metadata={
                        "query_type": "activity_list",
                        "start_date": bounded.start_iso,
                        "end_date": bounded.end_iso,
                        "activity_type": activity_type or "all",
                        "unit": unit,
                    },
                    pagination={
                        "cursor": preview_cursor,
                        "has_more": True,
                        "limit": page_size,
                        "returned": len(activities),
                        "partial": True,
                        "truncation_reason": "response_bytes",
                    },
                    surface=surface,
                    policy_context={"include_location": include_location},
                )
                if preview_size > budget.policy.max_response_bytes:
                    if len(activities) == 1:
                        budget.record_response_size(preview_size)
                        raise AssertionError("Oversized response was accepted by its budget")
                    activities.pop()
                    activity_positions.pop()
                    next_position = scan_position - 1
                    truncation_reason = "response_bytes"
                    response_limited = True
                    break
            if len(activities) > page_size:
                next_position = scan_position - 1
                truncation_reason = "page_limit"
                break
        if len(activities) > page_size:
            break
        if response_limited:
            break
        if len(batch) < upstream_page_size:
            exhausted = True

    has_more = next_position is not None

    def filtered_activity_cursor(cursor_position: int) -> str:
        validate_continuation_headroom(cursor_position, maximum_scan_advance)
        return encode_continuation_cursor(
            surface=surface,
            position=cursor_position,
            page_size=page_size,
            filters=filters,
        )

    if has_more:
        next_cursor = filtered_activity_cursor(next_position)
    else:
        next_cursor = None
    activities = activities[:page_size]
    activity_positions = activity_positions[:page_size]
    pagination: dict[str, Any] = {
        "cursor": next_cursor,
        "has_more": has_more,
        "limit": page_size,
        "returned": len(activities),
    }
    if has_more:
        pagination.update(
            partial=True,
            truncation_reason=truncation_reason or "page_limit",
        )
        if budget is not None:
            budget.note_truncation(
                {
                    "page_limit": "page_items",
                    "api_call_budget": "api_calls",
                    "response_bytes": "response_bytes",
                }[truncation_reason or "page_limit"]
            )

    if not activities:
        type_msg = f" of type '{activity_type}'" if activity_type else ""
        return ResponseBuilder.build_response(
            data={"activities": [], "count": 0},
            metadata={
                "query_type": "activity_list",
                "start_date": bounded.start_iso,
                "end_date": bounded.end_iso,
                "activity_type": activity_type or "all",
                "unit": unit,
            },
            pagination=pagination,
            analysis={
                "insights": [f"No activities found{type_msg} between {start_date} and {end_date}"]
            },
            surface="query_activities",
            policy_context={"include_location": include_location},
        )

    return ResponseBuilder.build_bounded_collection_response(
        items=activities,
        data_factory=lambda values: {
            "activities": [ResponseBuilder.format_activity(act, unit) for act in values],
            "aggregated": ResponseBuilder.aggregate_activities(values, unit),
        },
        metadata_factory=lambda _count: {
            "query_type": "activity_list",
            "start_date": bounded.start_iso,
            "end_date": bounded.end_iso,
            "activity_type": activity_type or "all",
            "unit": unit,
        },
        pagination=pagination,
        cursor_factory=lambda count: filtered_activity_cursor(activity_positions[count - 1]),
        surface="query_activities",
        policy_context={"include_location": include_location},
    )


async def _query_activities_general_paginated(
    ctx: Context,
    activity_type: str,
    cursor: str | None,
    limit: str | int | None,
    unit: UnitSystem,
    include_location: bool,
) -> str:
    """Query activities with general pagination (no date filter)."""
    surface = "query_activities"
    policy = policy_for_surface(surface)
    filters = {
        "activity_type": activity_type,
        "unit": unit,
        "include_location": include_location,
    }
    if cursor is None:
        start_index = 0
        page_size = validate_page_size(limit, policy)
    else:
        try:
            start_index, page_size = decode_continuation_cursor(
                cursor,
                surface=surface,
                filters=filters,
                policy=policy,
            )
        except ValueError as exc:
            raise InvalidContinuationCursorError from exc
        if limit is not None and validate_page_size(limit, policy) != page_size:
            raise InvalidContinuationCursorError

    client = await ctx.get_state("client")
    fetch_limit = page_size + 1
    activities = await client.call(
        "get_activities",
        start_index,
        fetch_limit,
        activity_type or None,
    )
    if not isinstance(activities, list):
        activities = []

    # Check if there are more results
    has_more = len(activities) > page_size
    activities = activities[:page_size]

    def general_activity_cursor(count: int) -> str:
        next_position = start_index + count
        validate_continuation_headroom(next_position, (2 * page_size) + 1)
        return encode_continuation_cursor(
            surface=surface,
            position=next_position,
            page_size=page_size,
            filters=filters,
        )

    next_cursor = general_activity_cursor(len(activities)) if has_more else None
    pagination: dict[str, Any] = {
        "cursor": next_cursor,
        "has_more": has_more,
        "limit": page_size,
        "returned": len(activities),
    }
    if has_more:
        pagination.update(partial=True, truncation_reason="page_limit")
        budget = current_request_budget()
        if budget is not None:
            budget.note_truncation("page_items")

    if not activities:
        type_msg = f" of type '{activity_type}'" if activity_type else ""
        return ResponseBuilder.build_response(
            data={"activities": [], "count": 0},
            metadata={
                "query_type": "activity_list",
                "activity_type": activity_type or "all",
                "unit": unit,
            },
            pagination=pagination,
            analysis={"insights": [f"No activities found{type_msg}"]},
            surface="query_activities",
            policy_context={"include_location": include_location},
        )

    return ResponseBuilder.build_bounded_collection_response(
        items=activities,
        data_factory=lambda values: {
            "activities": [ResponseBuilder.format_activity(act, unit) for act in values],
            "aggregated": ResponseBuilder.aggregate_activities(values, unit),
        },
        metadata_factory=lambda _count: {
            "query_type": "activity_list",
            "activity_type": activity_type or "all",
            "unit": unit,
        },
        pagination=pagination,
        cursor_factory=general_activity_cursor,
        surface="query_activities",
        policy_context={"include_location": include_location},
    )


async def query_activities(
    activity_id: Annotated[int | None, "Specific activity ID to retrieve"] = None,
    start_date: Annotated[str | None, "Start date in YYYY-MM-DD format for range query"] = None,
    end_date: Annotated[str | None, "End date in YYYY-MM-DD format for range query"] = None,
    date: Annotated[str | None, "Specific date in YYYY-MM-DD format or 'today'/'yesterday'"] = None,
    cursor: Annotated[
        str | None, "Pagination cursor from previous response (for continuing multi-page queries)"
    ] = None,
    limit: Annotated[
        str | int | None,
        "Maximum activities per page (1-50). Default: 20. "
        "Use pagination cursor for large datasets.",
    ] = None,
    activity_type: Annotated[str, "Activity type filter (e.g., 'running', 'cycling')"] = "",
    include_location: Annotated[
        bool,
        "Include exact start/end coordinates; disabled by default because location is sensitive",
    ] = False,
    unit: Annotated[UnitSystem, "Unit system: 'metric' or 'imperial'"] = "metric",
    ctx: Context | None = None,
) -> str:
    """
    Query activities with flexible parameters and pagination support.

    This unified tool supports multiple query patterns:
    1. Get specific activity: provide activity_id
    2. Get activities by date range: provide start_date and end_date (paginated)
    3. Get activities for specific date: provide date
    4. Get paginated activities: use cursor and limit
    5. Get last activity: no parameters

    All queries can be filtered by activity_type (e.g., 'running', 'cycling').

    Pagination:
    For large time ranges, use pagination to retrieve all activities:
    1. Make initial request without cursor
    2. Check response["pagination"]["has_more"]
    3. Use response["pagination"]["cursor"] for next page

    Returns: JSON string with structure:
    {
        "data": {
            "activity": {...}       // Single activity mode
            OR
            "activities": [...],    // List mode
            "count": N
        },
        "pagination": {             // List mode only (when paginated)
            "cursor": "...",        // Use for next page (null if no more)
            "has_more": true,
            "limit": 20,
            "returned": 20
        },
        "metadata": {...}
    }
    """
    assert ctx is not None
    try:
        if activity_id is not None and any(
            value is not None for value in (start_date, end_date, date, cursor, limit)
        ):
            return ResponseBuilder.build_error_response(
                "activity_id cannot be combined with date, range, cursor, or limit",
                "invalid_parameters",
            )
        if date is not None and (start_date is not None or end_date is not None):
            return ResponseBuilder.build_error_response(
                "date cannot be combined with a date range",
                "invalid_parameters",
            )
        if (start_date is None) != (end_date is None):
            from ..query_budget import InvalidDateRangeError

            raise InvalidDateRangeError
        if start_date is not None:
            validate_date_range(
                start_date,
                end_date,
                policy=policy_for_surface("query_activities"),
            )
        normalized_date = (
            validate_date_range(
                date,
                date,
                policy=policy_for_surface("query_activities"),
            ).start_iso
            if date is not None
            else None
        )
        # Pattern 1: Specific activity by ID
        if activity_id is not None:
            client = await ctx.get_state("client")
            activity = await client.call("get_activity", activity_id)

            if not activity:
                return ResponseBuilder.build_error_response(
                    f"Activity {activity_id} not found",
                    "not_found",
                    [
                        "Check that the activity ID is correct",
                        "Try query_activities() to list recent activities",
                    ],
                )

            # Format the activity with rich data
            formatted_activity = ResponseBuilder.format_activity(activity, unit)
            single_data = {"activity": formatted_activity}
            reserve_projected_response_items(
                "query_activities",
                single_data,
                {"include_location": include_location},
            )

            return ResponseBuilder.build_response(
                data=single_data,
                metadata={
                    "query_type": "single_activity",
                    "activity_id": activity_id,
                    "unit": unit,
                },
                surface="query_activities",
                policy_context={"include_location": include_location},
            )

        # Pattern 2: Date range query (with pagination)
        if start_date and end_date:
            return await _query_activities_paginated(
                ctx=ctx,
                start_date=start_date,
                end_date=end_date,
                activity_type=activity_type,
                cursor=cursor,
                limit=limit,
                unit=unit,
                include_location=include_location,
            )

        # Pattern 3: Specific date query
        if normalized_date is not None:
            return await _query_activities_paginated(
                ctx=ctx,
                start_date=normalized_date,
                end_date=normalized_date,
                activity_type=activity_type,
                cursor=cursor,
                limit=limit,
                unit=unit,
                include_location=include_location,
            )

        # Pattern 4: Pagination query (general pagination using Garmin's start/limit API)
        if cursor is not None or limit is not None:
            # Use cursor-based pagination for general queries
            return await _query_activities_general_paginated(
                ctx=ctx,
                activity_type=activity_type,
                cursor=cursor,
                limit=limit,
                unit=unit,
                include_location=include_location,
            )

        # Pattern 5: Last activity (default)
        client = await ctx.get_state("client")
        activity = await client.call("get_last_activity")

        if not activity:
            return ResponseBuilder.build_response(
                data={"activity": None},
                analysis={"insights": ["No activities found"]},
                surface="query_activities",
                policy_context={"include_location": include_location},
            )

        formatted_activity = ResponseBuilder.format_activity(activity, unit)
        last_data = {"activity": formatted_activity}
        reserve_projected_response_items(
            "query_activities",
            last_data,
            {"include_location": include_location},
        )

        return ResponseBuilder.build_response(
            data=last_data,
            metadata={"query_type": "last_activity", "unit": unit},
            surface="query_activities",
            policy_context={"include_location": include_location},
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


def _compute_accurate_splits_from_details(
    activity_details: dict[str, Any], unit: UnitSystem = "metric"
) -> dict[str, Any]:
    """
    Compute accurate distance splits from time-series data in activity details.

    Uses actual GPS/sensor data to determine exact times when each km/mile was crossed.
    This provides real split times showing pace variation throughout the activity.

    Args:
        activity_details: Activity details from get_activity_details() API
        unit: Unit system - "metric" for 1km splits, "imperial" for 1 mile splits

    Returns:
        Dictionary with accurate splits information or error details
    """
    # Extract metric descriptors and data
    if (
        "activityDetailMetrics" not in activity_details
        or "metricDescriptors" not in activity_details
    ):
        return {"accurate": False, "reason": "No time-series metric data available"}

    metrics_data = activity_details["activityDetailMetrics"]
    if not metrics_data:
        return {"accurate": False, "reason": "Empty metrics data"}

    # Find indices for distance and time metrics
    # Index 12 = sumDistance (cumulative distance in meters)
    # Index 8 = sumDuration (cumulative time in seconds)
    DISTANCE_INDEX = 12
    TIME_INDEX = 8

    # Extract time/distance pairs
    time_distance_pairs = []
    for entry in metrics_data:
        metrics = entry.get("metrics", [])
        if len(metrics) > max(DISTANCE_INDEX, TIME_INDEX):
            distance = metrics[DISTANCE_INDEX]
            time = metrics[TIME_INDEX]
            if distance is not None and time is not None:
                time_distance_pairs.append((float(time), float(distance)))

    if len(time_distance_pairs) < 2:
        return {"accurate": False, "reason": "Insufficient time-series data points"}

    # Sort by time (should already be sorted, but ensure it)
    time_distance_pairs.sort(key=lambda x: x[0])

    # Determine split distance based on unit
    split_distance_meters = 1000 if unit == "metric" else 1609.34  # 1 mile
    split_label = "km" if unit == "metric" else "mi"

    # Get total distance from last data point
    total_distance = time_distance_pairs[-1][1]

    if total_distance < split_distance_meters:
        return {"accurate": False, "reason": f"Activity distance less than 1 {split_label}"}

    # Calculate number of complete splits
    num_complete_splits = int(total_distance // split_distance_meters)

    # Function to interpolate time for a given distance
    def find_time_at_distance(target_distance: float) -> float:
        """Find time when target distance was reached using linear interpolation."""
        for i in range(len(time_distance_pairs) - 1):
            time1, dist1 = time_distance_pairs[i]
            time2, dist2 = time_distance_pairs[i + 1]

            if dist1 <= target_distance <= dist2:
                # Linear interpolation
                if dist2 - dist1 == 0:
                    return time1
                ratio = (target_distance - dist1) / (dist2 - dist1)
                return time1 + ratio * (time2 - time1)

        # If not found, return last time (shouldn't happen)
        return time_distance_pairs[-1][0]

    # Calculate splits
    splits = []
    prev_time = 0.0

    for split_num in range(1, num_complete_splits + 1):
        split_distance = split_num * split_distance_meters
        split_time = find_time_at_distance(split_distance)
        segment_time = split_time - prev_time

        # Calculate pace for this segment
        pace_seconds = segment_time  # Time for 1km or 1 mile
        pace_minutes = int(pace_seconds // 60)
        pace_secs = int(pace_seconds % 60)

        # Format distance based on unit
        if unit == "metric":
            distance_display = split_distance_meters / 1000  # km
        else:
            distance_display = split_distance_meters / 1609.34  # miles

        splits.append(
            {
                "split_number": split_num,
                "distance_meters": split_distance_meters,
                "distance_formatted": f"{distance_display:.2f} {split_label}",
                "time_seconds": segment_time,
                "cumulative_time_seconds": split_time,
                "time_formatted": ResponseBuilder._format_duration(segment_time),
                "pace_formatted": f"{pace_minutes}:{pace_secs:02d} /{split_label}",
            }
        )

        prev_time = split_time

    # Add partial split if there's significant remaining distance
    remaining_distance = total_distance - (num_complete_splits * split_distance_meters)
    if remaining_distance >= 100:  # Only if >= 100m
        final_time = time_distance_pairs[-1][0]
        partial_segment_time = final_time - prev_time

        # Calculate pace (extrapolate to full km/mile)
        if remaining_distance > 0:
            pace_per_full_unit = (partial_segment_time / remaining_distance) * split_distance_meters
            pace_minutes = int(pace_per_full_unit // 60)
            pace_secs = int(pace_per_full_unit % 60)
            pace_str = f"{pace_minutes}:{pace_secs:02d} /{split_label}"
        else:
            pace_str = "N/A"

        # Format partial distance
        if unit == "metric":
            partial_distance_display = remaining_distance / 1000  # km
        else:
            partial_distance_display = remaining_distance / 1609.34  # miles

        splits.append(
            {
                "split_number": num_complete_splits + 1,
                "distance_meters": remaining_distance,
                "distance_formatted": f"{partial_distance_display:.2f} {split_label}",
                "time_seconds": partial_segment_time,
                "cumulative_time_seconds": final_time,
                "time_formatted": ResponseBuilder._format_duration(partial_segment_time),
                "pace_formatted": pace_str,
                "partial": True,
            }
        )

    # Calculate average pace
    total_time = time_distance_pairs[-1][0]
    avg_pace_per_km = (total_time / total_distance) * 1000

    return {
        "accurate": True,
        "note": f"Accurate {split_label} splits computed from GPS/sensor data (1398 data points)",
        "average_pace": {
            "seconds_per_km": avg_pace_per_km,
            "formatted": ResponseBuilder._format_pace(total_distance / total_time, unit),
        },
        "splits": splits,
        "total_distance_meters": total_distance,
        "total_duration_seconds": total_time,
        "data_points": len(time_distance_pairs),
    }


def _compute_estimated_splits(
    activity: dict[str, Any], unit: UnitSystem = "metric"
) -> dict[str, Any]:
    """
    Compute estimated distance splits when activity has only 1 lap.

    Computes 1km splits for metric units or 1 mile splits for imperial units.
    This provides estimated split times based on average pace,
    useful when the watch wasn't configured for auto-lap.

    Args:
        activity: Activity data containing distance and duration
        unit: Unit system - "metric" for 1km splits, "imperial" for 1 mile splits

    Returns:
        Dictionary with estimated splits information
    """
    distance_meters = activity.get("distance")
    duration_seconds = activity.get("duration")

    if not distance_meters or not duration_seconds:
        return {"estimated": False, "reason": "Missing distance or duration data"}

    # Only compute for activities >= 1km
    if distance_meters < 1000:
        return {"estimated": False, "reason": "Activity distance less than 1km"}

    # Calculate average pace (seconds per km)
    avg_pace_per_km = (duration_seconds / distance_meters) * 1000

    # Determine split distance based on unit system
    split_distance_meters = 1000 if unit == "metric" else 1609.34  # 1 mile
    split_label = "km" if unit == "metric" else "mi"

    # Calculate number of complete splits
    num_complete_splits = int(distance_meters // split_distance_meters)

    if num_complete_splits == 0:
        return {"estimated": False, "reason": f"Activity distance less than 1 {split_label}"}

    # Calculate remaining distance
    remaining_distance = distance_meters - (num_complete_splits * split_distance_meters)

    # Build estimated splits
    estimated_splits = []
    for i in range(1, num_complete_splits + 1):
        split_time_seconds = avg_pace_per_km * (split_distance_meters / 1000)

        # Format pace
        minutes = int(split_time_seconds // 60)
        seconds = int(split_time_seconds % 60)

        # Format distance based on unit
        if unit == "metric":
            distance_display = split_distance_meters / 1000  # km
        else:
            distance_display = split_distance_meters / 1609.34  # miles

        estimated_splits.append(
            {
                "split_number": i,
                "distance_meters": split_distance_meters,
                "distance_formatted": f"{distance_display:.2f} {split_label}",
                "time_seconds": split_time_seconds,
                "time_formatted": ResponseBuilder._format_duration(split_time_seconds),
                "pace_formatted": f"{minutes}:{seconds:02d} /{split_label}",
            }
        )

    # Add partial split if there's remaining distance
    if remaining_distance >= 100:  # Only include if >= 100m
        partial_split_time = avg_pace_per_km * (remaining_distance / 1000)

        # Format partial distance based on unit
        if unit == "metric":
            partial_distance_display = remaining_distance / 1000  # km
        else:
            partial_distance_display = remaining_distance / 1609.34  # miles

        estimated_splits.append(
            {
                "split_number": num_complete_splits + 1,
                "distance_meters": remaining_distance,
                "distance_formatted": f"{partial_distance_display:.2f} {split_label}",
                "time_seconds": partial_split_time,
                "time_formatted": ResponseBuilder._format_duration(partial_split_time),
                "pace_formatted": f"{int(avg_pace_per_km // 60)}:{int(avg_pace_per_km % 60):02d} /{split_label} (avg)",
                "partial": True,
            }
        )

    return {
        "estimated": True,
        "note": f"Estimated {split_label} splits based on average pace (activity had only 1 lap)",
        "average_pace": {
            "seconds_per_km": avg_pace_per_km,
            "formatted": ResponseBuilder._format_pace(distance_meters / duration_seconds, unit),
        },
        "splits": estimated_splits,
        "total_distance_meters": distance_meters,
        "total_duration_seconds": duration_seconds,
    }


async def get_activity_details(
    activity_id: Annotated[int, "Activity ID"],
    include_splits: Annotated[bool, "Include lap/split data"] = True,
    include_weather: Annotated[bool, "Include weather conditions"] = True,
    include_hr_zones: Annotated[bool, "Include heart rate zone data"] = True,
    include_gear: Annotated[bool, "Include gear information"] = True,
    include_exercise_sets: Annotated[bool, "Include exercise sets (for strength training)"] = False,
    include_location: Annotated[
        bool,
        "Include exact start/end coordinates; disabled by default because location is sensitive",
    ] = False,
    unit: Annotated[UnitSystem, "Unit system: 'metric' or 'imperial'"] = "metric",
    ctx: Context | None = None,
) -> str:
    """
    Get comprehensive details for a specific activity.

    Fetch exactly the information you need about an activity with flexible
    detail options.

    By default, includes splits, weather, HR zones, and gear. Exercise sets
    are only included when explicitly requested (useful for strength training).

    When include_splits=True and the activity has only 1 lap, estimated km/mile
    splits will be computed based on average pace.
    """
    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        # Start with base activity data
        activity = await client.call("get_activity", activity_id)

        if not activity:
            return ResponseBuilder.build_error_response(
                f"Activity {activity_id} not found",
                "not_found",
                [
                    "Check that the activity ID is correct",
                    "Try query_activities() to list recent activities",
                ],
            )

        # Format base activity
        formatted_activity = ResponseBuilder.format_activity(activity, unit)
        details: dict = {"activity": formatted_activity}

        # Fetch optional details
        if include_splits:
            try:
                splits = await client.call("get_activity_splits", activity_id)
                details["splits"] = splits

                # If only 1 lap, try to compute accurate splits from detailed time-series data
                if splits and "lapDTOs" in splits and len(splits["lapDTOs"]) == 1:
                    # Try to get accurate splits from activity details API
                    try:
                        activity_details = await client.call(
                            "get_activity_details", activity_id, maxchart=2000
                        )
                        accurate_splits = _compute_accurate_splits_from_details(
                            activity_details, unit
                        )

                        if accurate_splits.get("accurate"):
                            # We got accurate splits from GPS/sensor data!
                            details["computed_splits"] = accurate_splits
                        else:
                            # Fall back to estimated even-pace splits
                            estimated_splits = _compute_estimated_splits(activity, unit)
                            if estimated_splits.get("estimated"):
                                details["computed_splits"] = estimated_splits
                    except QueryBudgetError:
                        raise
                    except Exception:
                        # If details API fails, fall back to estimated splits
                        estimated_splits = _compute_estimated_splits(activity, unit)
                        if estimated_splits.get("estimated"):
                            details["computed_splits"] = estimated_splits

            except QueryBudgetError:
                raise
            except Exception:
                details["splits"] = None

        if include_weather:
            try:
                weather = await client.call("get_activity_weather", activity_id)
                details["weather"] = weather
            except QueryBudgetError:
                raise
            except Exception:
                details["weather"] = None

        if include_hr_zones:
            try:
                hr_zones = await client.call("get_activity_hr_in_timezones", activity_id)
                details["hr_zones"] = hr_zones
            except QueryBudgetError:
                raise
            except Exception:
                details["hr_zones"] = None

        if include_gear:
            try:
                gear = await client.call("get_activity_gear", activity_id)
                details["gear"] = gear
            except QueryBudgetError:
                raise
            except Exception:
                details["gear"] = None

        if include_exercise_sets:
            try:
                sets = await client.call("get_activity_exercise_sets", activity_id)
                details["exercise_sets"] = sets
            except QueryBudgetError:
                raise
            except Exception:
                details["exercise_sets"] = None

        # Generate insights based on available data
        insights = []
        if details.get("weather"):
            insights.append("Weather data available for this activity")
        if details.get("hr_zones"):
            insights.append("Heart rate zone distribution available")
        if details.get("splits"):
            insights.append("Lap/split data available for pace analysis")
        if details.get("computed_splits"):
            computed = details["computed_splits"]
            split_count = len(computed.get("splits", []))
            split_unit = "km" if unit == "metric" else "mile"

            if computed.get("accurate"):
                data_points = computed.get("data_points", 0)
                insights.append(
                    f"Accurate {split_count} × 1{split_unit} splits computed from {data_points} GPS/sensor data points"
                )
            elif computed.get("estimated"):
                insights.append(
                    f"Estimated {split_count} × 1{split_unit} splits computed from average pace"
                )
        if details.get("gear"):
            insights.append("Gear information recorded for this activity")

        reserve_projected_response_items(
            "get_activity_details",
            details,
            {"include_location": include_location},
        )
        return ResponseBuilder.build_response(
            data=details,
            analysis={"insights": insights} if insights else None,
            metadata={
                "query_type": "activity_details",
                "activity_id": activity_id,
                "unit": unit,
                "includes": {
                    "splits": include_splits,
                    "weather": include_weather,
                    "hr_zones": include_hr_zones,
                    "gear": include_gear,
                    "exercise_sets": include_exercise_sets,
                },
            },
            surface="get_activity_details",
            policy_context={"include_location": include_location},
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def get_activity_social(
    activity_id: Annotated[int, "Activity ID to get social details for"],
    ctx: Context | None = None,
) -> str:
    """
    Get social details for an activity (likes, comments, kudos).

    Args:
        activity_id: The Garmin Connect activity ID

    Returns:
        Structured JSON with social data, analysis, and metadata
    """
    del activity_id, ctx
    return ResponseBuilder.build_error_response(
        unavailable_message("get_activity_social"),
        "capability_unavailable",
        ["Use get_activity_details for the supported activity data available in this version"],
    )
