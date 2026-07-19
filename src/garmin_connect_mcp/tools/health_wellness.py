"""Health and wellness tools for Garmin Connect MCP server."""

from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import Context

from ..client import GarminAPIError
from ..pagination import (
    PaginationInfo,
    encode_continuation_cursor,
    paginate_date_range,
)
from ..query_budget import (
    InvalidContinuationCursorError,
    InvalidDateRangeError,
    InvalidPageSizeError,
    QueryBudgetError,
    current_request_budget,
    policy_for_surface,
    validate_date_range,
)
from ..response_builder import ResponseBuilder
from ..time_utils import parse_date_string
from ..types import UnitSystem


def _query_date_page(
    *,
    surface: str,
    date_value: str | None,
    start_date: str | None,
    end_date: str | None,
    cursor: str | None,
    limit: str | int | None,
    default_date: str,
    filter_values: dict[str, Any] | None = None,
) -> tuple[tuple[str, ...], PaginationInfo | None, bool, int, dict[str, Any]]:
    """Validate one date selection and return a bounded page."""
    if date_value is not None:
        if start_date is not None or end_date is not None or cursor is not None:
            raise InvalidDateRangeError
        try:
            normalized = parse_date_string(date_value).strftime("%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise InvalidDateRangeError from exc
        return (normalized,), None, False, 0, {}
    if (start_date is None) != (end_date is None):
        raise InvalidDateRangeError
    if start_date is None:
        if cursor is not None:
            raise InvalidContinuationCursorError
        try:
            normalized = parse_date_string(default_date).strftime("%Y-%m-%d")
        except (TypeError, ValueError) as exc:
            raise InvalidDateRangeError from exc
        return (normalized,), None, False, 0, {}

    policy = policy_for_surface(surface)
    bounded = validate_date_range(start_date, end_date, policy=policy)
    filters: dict[str, Any] = {
        "start_date": bounded.start_iso,
        "end_date": bounded.end_iso,
    }
    filters.update(filter_values or {})
    dates, pagination, position = paginate_date_range(
        bounded,
        surface=surface,
        filters=filters,
        policy=policy,
        cursor=cursor,
        requested_page_size=limit,
    )
    if pagination["has_more"]:
        pagination["partial"] = True
        pagination["truncation_reason"] = "page_limit"
        budget = current_request_budget()
        if budget is not None:
            budget.note_truncation("page_items")
    return dates, pagination, True, position, filters


def _require_calls(count: int) -> None:
    budget = current_request_budget()
    if budget is not None:
        budget.require_calls(count)


def _stop_before_next_date_for_response_budget(
    *,
    items: list[Any],
    data_factory: Callable[[list[Any]], Any],
    metadata_factory: Callable[[int], dict[str, Any]],
    pagination: PaginationInfo | None,
    surface: str,
    page_position: int,
    page_filters: dict[str, Any],
) -> bool:
    """Stop a daily fan-out once the newest whole item cannot fit."""
    budget = current_request_budget()
    if budget is None or pagination is None or not items:
        return False
    count = len(items)
    has_more = count < pagination["returned"] or pagination["has_more"]
    preview_page: PaginationInfo = {
        "cursor": (
            encode_continuation_cursor(
                surface=surface,
                position=page_position + count,
                page_size=pagination["limit"],
                filters=page_filters,
            )
            if has_more
            else None
        ),
        "has_more": has_more,
        "limit": pagination["limit"],
        "returned": count,
    }
    if has_more:
        preview_page["partial"] = True
        preview_page["truncation_reason"] = "page_limit"
    size = ResponseBuilder.preview_response_size(
        data=data_factory(items),
        metadata=metadata_factory(count),
        pagination=preview_page,
        surface=surface,
    )
    if size <= budget.policy.max_response_bytes:
        return False
    if count == 1:
        budget.record_response_size(size)
        raise AssertionError("Oversized response was accepted by its budget")

    items.pop()
    returned = len(items)
    pagination.update(
        cursor=encode_continuation_cursor(
            surface=surface,
            position=page_position + returned,
            page_size=pagination["limit"],
            filters=page_filters,
        ),
        has_more=True,
        returned=returned,
        partial=True,
        truncation_reason="response_bytes",
    )
    budget.note_truncation("response_bytes")
    return True


async def query_health_summary(
    date: Annotated[str | None, "Specific date ('today', 'yesterday', or YYYY-MM-DD)"] = None,
    start_date: Annotated[str | None, "Range start date (YYYY-MM-DD)"] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD)"] = None,
    cursor: Annotated[
        str | None, "Pagination cursor from previous response (for multi-day ranges)"
    ] = None,
    limit: Annotated[
        str | int | None,
        "Maximum days per page (1-30). Default: 7. Use cursor for large date ranges.",
    ] = None,
    include_body_battery: Annotated[bool, "Include Body Battery data"] = True,
    include_training_readiness: Annotated[bool, "Include training readiness"] = True,
    include_training_status: Annotated[bool, "Include training status"] = True,
    unit: Annotated[UnitSystem, "Unit system: 'metric' or 'imperial'"] = "metric",
    ctx: Context | None = None,
) -> str:
    """
    Get comprehensive daily health snapshot with pagination support.

    Includes stats, user summary, training readiness, training status,
    Body Battery, and Body Battery events.

    Supports single date or date range queries with pagination.

    Pagination:
    For large date ranges, use pagination:
    1. Make initial request with start_date and end_date
    2. Check response["pagination"]["has_more"]
    3. Use response["pagination"]["cursor"] for next page

    Returns: JSON string with structure:
    {
        "data": {
            "summaries": [...],  // Range mode (paginated)
            "count": N
            OR
            {...}                // Single date mode
        },
        "pagination": {          // Range mode only
            "cursor": "...",
            "has_more": true,
            "limit": 30,
            "returned": 30
        },
        "metadata": {...}
    }
    """
    assert ctx is not None
    try:
        dates, pagination, is_range, page_position, page_filters = _query_date_page(
            surface="query_health_summary",
            date_value=date,
            start_date=start_date,
            end_date=end_date,
            cursor=cursor,
            limit=limit,
            default_date="today",
            filter_values={
                "include_body_battery": include_body_battery,
                "include_training_readiness": include_training_readiness,
                "include_training_status": include_training_status,
                "unit": unit,
            },
        )
        calls_per_day = (
            2
            + int(include_training_readiness)
            + int(include_training_status)
            + (2 * int(include_body_battery))
        )
        _require_calls(len(dates) * calls_per_day)
        client = await ctx.get_state("client")

        # Collect data for each date
        summaries = []
        for date_str in dates:
            summary = {"date": ResponseBuilder.format_date_with_day(date_str)}

            # Get base stats
            try:
                stats = await client.call("get_stats", date_str)
                summary["stats"] = stats
            except QueryBudgetError:
                raise
            except Exception:
                summary["stats"] = None

            # Get user summary
            try:
                user_summary = await client.call("get_user_summary", date_str)
                summary["user_summary"] = user_summary
            except QueryBudgetError:
                raise
            except Exception:
                summary["user_summary"] = None

            # Training readiness
            if include_training_readiness:
                try:
                    readiness = await client.call("get_training_readiness", date_str)
                    summary["training_readiness"] = readiness
                except QueryBudgetError:
                    raise
                except Exception:
                    summary["training_readiness"] = None

            # Training status
            if include_training_status:
                try:
                    status = await client.call("get_training_status", date_str)
                    summary["training_status"] = status
                except QueryBudgetError:
                    raise
                except Exception:
                    summary["training_status"] = None

            # Body battery
            if include_body_battery:
                try:
                    # Body battery typically needs a range
                    bb = await client.call("get_body_battery", date_str, date_str)
                    summary["body_battery"] = bb
                except QueryBudgetError:
                    raise
                except Exception:
                    summary["body_battery"] = None

                try:
                    bb_events = await client.call("get_body_battery_events", date_str)
                    summary["body_battery_events"] = bb_events
                except QueryBudgetError:
                    raise
                except Exception:
                    summary["body_battery_events"] = None

            summaries.append(summary)
            if _stop_before_next_date_for_response_budget(
                items=summaries,
                data_factory=lambda values: {
                    "summaries": values,
                    "count": len(values),
                },
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                    "unit": unit,
                },
                pagination=pagination,
                surface="query_health_summary",
                page_position=page_position,
                page_filters=page_filters,
            ):
                break
        # Build insights
        insights = []
        if len(summaries) == 1:
            s = summaries[0]
            if s.get("training_readiness"):
                insights.append("Training readiness data available")
            if s.get("body_battery"):
                insights.append("Body Battery tracking available")
        else:
            insights.append(f"Health summary for {len(summaries)} days")

        # Return appropriate structure
        if is_range:
            assert pagination is not None
            return ResponseBuilder.build_bounded_collection_response(
                items=summaries,
                data_factory=lambda values: {"summaries": values, "count": len(values)},
                analysis={"insights": insights} if insights else None,
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                    "unit": unit,
                },
                pagination=pagination,
                cursor_factory=lambda count: encode_continuation_cursor(
                    surface="query_health_summary",
                    position=page_position + count,
                    page_size=pagination["limit"],
                    filters=page_filters,
                ),
                surface="query_health_summary",
            )
        else:
            budget = current_request_budget()
            if budget is not None:
                budget.reserve_items(len(summaries))
            return ResponseBuilder.build_response(
                data=summaries[0] if summaries else {},
                analysis={"insights": insights} if insights else None,
                metadata={"date": dates[0] if dates else None, "unit": unit},
                surface="query_health_summary",
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def query_sleep_data(
    date: Annotated[str | None, "Specific date ('today', 'yesterday', or YYYY-MM-DD)"] = None,
    start_date: Annotated[str | None, "Range start date (YYYY-MM-DD)"] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD)"] = None,
    cursor: Annotated[str | None, "Continuation cursor for a multi-day range"] = None,
    limit: Annotated[
        str | int | None,
        "Maximum days per page (1-31). Default: 7.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """
    Get sleep data and analysis.

    Retrieves sleep duration, sleep stages (deep, light, REM), sleep scores,
    HRV, resting heart rate, and body battery impact.

    Supports one date or a bounded range of at most 31 days. Range responses
    use stable continuation cursors and return at most 7 days by default.
    """
    assert ctx is not None
    try:
        dates, pagination, is_range, page_position, page_filters = _query_date_page(
            surface="query_sleep_data",
            date_value=date,
            start_date=start_date,
            end_date=end_date,
            cursor=cursor,
            limit=limit,
            default_date="yesterday",
        )
        _require_calls(len(dates))
        client = await ctx.get_state("client")

        # Collect sleep data
        sleep_data = []
        for date_str in dates:
            data = await client.call("get_sleep_data", date_str)
            sleep_data.append(
                {"date": ResponseBuilder.format_date_with_day(date_str), "sleep": data}
            )
            if _stop_before_next_date_for_response_budget(
                items=sleep_data,
                data_factory=lambda values: {
                    "sleep_data": values,
                    "count": len(values),
                },
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                },
                pagination=pagination,
                surface="query_sleep_data",
                page_position=page_position,
                page_filters=page_filters,
            ):
                break
        if not sleep_data:
            return ResponseBuilder.build_response(
                data={"sleep_data": []},
                analysis={"insights": ["No sleep data found for the specified period"]},
                metadata={"dates": dates},
                pagination=pagination,
                surface="query_sleep_data",
            )

        # Generate insights
        insights = []
        if len(sleep_data) == 1:
            data = sleep_data[0].get("sleep", {})
            dto = data.get("dailySleepDTO", {})
            total_hours = (dto.get("sleepTimeSeconds", 0)) / 3600
            sleep_score = dto.get("sleepScores", {}).get("overall", {}).get("value")

            if total_hours > 0:
                insights.append(f"Total sleep: {total_hours:.1f} hours")
            if sleep_score:
                insights.append(f"Sleep score: {sleep_score}/100")
        else:
            avg_sleep = 0
            count = 0
            for entry in sleep_data:
                dto = entry.get("sleep", {}).get("dailySleepDTO", {})
                total_hours = (dto.get("sleepTimeSeconds", 0)) / 3600
                if total_hours > 0:
                    avg_sleep += total_hours
                    count += 1

            if count > 0:
                insights.append(
                    f"Average sleep: {avg_sleep / count:.1f} hours/night over {count} nights"
                )

        # Return appropriate structure
        if is_range:
            assert pagination is not None
            return ResponseBuilder.build_bounded_collection_response(
                items=sleep_data,
                data_factory=lambda values: {"sleep_data": values, "count": len(values)},
                analysis={"insights": insights} if insights else None,
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                },
                pagination=pagination,
                cursor_factory=lambda count: encode_continuation_cursor(
                    surface="query_sleep_data",
                    position=page_position + count,
                    page_size=pagination["limit"],
                    filters=page_filters,
                ),
                surface="query_sleep_data",
            )
        else:
            budget = current_request_budget()
            if budget is not None:
                budget.reserve_items(len(sleep_data))
            return ResponseBuilder.build_response(
                data=sleep_data[0] if sleep_data else {},
                analysis={"insights": insights} if insights else None,
                metadata={"date": dates[0] if dates else None},
                surface="query_sleep_data",
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def query_heart_rate_data(
    date: Annotated[str | None, "Specific date ('today', 'yesterday', or YYYY-MM-DD)"] = None,
    start_date: Annotated[str | None, "Range start date (YYYY-MM-DD)"] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD)"] = None,
    include_resting: Annotated[bool, "Include resting heart rate"] = True,
    cursor: Annotated[str | None, "Continuation cursor for a multi-day range"] = None,
    limit: Annotated[
        str | int | None,
        "Maximum days per page (1-31). Default: 7.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """
    Get heart rate data.

    Retrieves heart rate data including resting HR, average HR, min/max values.
    Supports one date or a bounded range of at most 31 days.
    """
    assert ctx is not None
    try:
        dates, pagination, is_range, page_position, page_filters = _query_date_page(
            surface="query_heart_rate_data",
            date_value=date,
            start_date=start_date,
            end_date=end_date,
            cursor=cursor,
            limit=limit,
            default_date="today",
            filter_values={"include_resting": include_resting},
        )
        _require_calls(len(dates) * (1 + int(include_resting)))
        client = await ctx.get_state("client")

        # Collect heart rate data
        hr_data = []
        for date_str in dates:
            entry = {"date": ResponseBuilder.format_date_with_day(date_str)}

            # Get HR data
            try:
                hr = await client.call("get_heart_rates", date_str)
                entry["heart_rate"] = hr
            except QueryBudgetError:
                raise
            except Exception:
                entry["heart_rate"] = None

            # Get resting HR
            if include_resting:
                try:
                    rhr = await client.call("get_rhr_day", date_str)
                    entry["resting_hr"] = rhr
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["resting_hr"] = None

            hr_data.append(entry)
            if _stop_before_next_date_for_response_budget(
                items=hr_data,
                data_factory=lambda values: {
                    "heart_rate_data": values,
                    "count": len(values),
                },
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                },
                pagination=pagination,
                surface="query_heart_rate_data",
                page_position=page_position,
                page_filters=page_filters,
            ):
                break
        # Generate insights
        insights = []
        if len(hr_data) == 1:
            entry = hr_data[0]
            rhr = entry.get("resting_hr")
            if rhr:
                insights.append("Resting heart rate data available")
        else:
            insights.append(f"Heart rate data for {len(hr_data)} days")

        # Return appropriate structure
        if is_range:
            assert pagination is not None
            return ResponseBuilder.build_bounded_collection_response(
                items=hr_data,
                data_factory=lambda values: {
                    "heart_rate_data": values,
                    "count": len(values),
                },
                analysis={"insights": insights} if insights else None,
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                },
                pagination=pagination,
                cursor_factory=lambda count: encode_continuation_cursor(
                    surface="query_heart_rate_data",
                    position=page_position + count,
                    page_size=pagination["limit"],
                    filters=page_filters,
                ),
                surface="query_heart_rate_data",
            )
        else:
            budget = current_request_budget()
            if budget is not None:
                budget.reserve_items(len(hr_data))
            return ResponseBuilder.build_response(
                data=hr_data[0] if hr_data else {},
                analysis={"insights": insights} if insights else None,
                metadata={"date": dates[0] if dates else None},
                surface="query_heart_rate_data",
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def query_activity_metrics(
    date: Annotated[str | None, "Specific date ('today', 'yesterday', or YYYY-MM-DD)"] = None,
    start_date: Annotated[str | None, "Range start date (YYYY-MM-DD)"] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD)"] = None,
    metrics: Annotated[
        str,
        "Comma-separated metrics: steps,stress,respiration,spo2,floors,hydration,blood_pressure,body_composition",
    ] = "steps,stress",
    unit: Annotated[UnitSystem, "Unit system: 'metric' or 'imperial'"] = "metric",
    cursor: Annotated[str | None, "Continuation cursor for a multi-day range"] = None,
    limit: Annotated[
        str | int | None,
        "Maximum days per page (1-31). Default: 7.",
    ] = None,
    ctx: Context | None = None,
) -> str:
    """
    Get activity metrics (steps, stress, etc.).

    Includes steps, stress, respiration, SpO2, floors climbed, hydration,
    blood pressure, and body composition.

    Select specific metrics to retrieve using the metrics parameter.
    Default: steps and stress.
    """
    assert ctx is not None
    try:
        # Parse requested metrics
        requested_metrics = list(dict.fromkeys(m.strip().lower() for m in metrics.split(",")))
        allowed_metrics = {
            "steps",
            "stress",
            "respiration",
            "spo2",
            "floors",
            "hydration",
            "blood_pressure",
            "body_composition",
        }
        if not requested_metrics or any(
            metric not in allowed_metrics for metric in requested_metrics
        ):
            return ResponseBuilder.build_error_response(
                "metrics must contain only documented metric names",
                "invalid_parameters",
            )
        range_backed_metrics = {
            "blood_pressure",
            "body_composition",
        }.intersection(requested_metrics)
        range_selected = start_date is not None or end_date is not None or cursor is not None
        if range_selected and range_backed_metrics and limit is not None:
            if isinstance(limit, bool):
                raise InvalidPageSizeError(1)
            try:
                requested_limit = int(limit)
            except (TypeError, ValueError) as exc:
                raise InvalidPageSizeError(1) from exc
            if requested_limit != 1:
                raise InvalidPageSizeError(1)
        # Range-backed Garmin methods return one aggregate window. Keeping such
        # pages to one day makes the payload, metadata, and continuation cursor
        # describe exactly the same interval even under byte pressure.
        effective_limit: str | int | None = 1 if range_selected and range_backed_metrics else limit
        dates, pagination, is_range, page_position, page_filters = _query_date_page(
            surface="query_activity_metrics",
            date_value=date,
            start_date=start_date,
            end_date=end_date,
            cursor=cursor,
            limit=effective_limit,
            default_date="today",
            filter_values={"metrics": requested_metrics, "unit": unit},
        )
        daily_metrics = {
            "steps",
            "stress",
            "respiration",
            "spo2",
            "floors",
            "hydration",
        }.intersection(requested_metrics)
        range_calls = int(is_range and "blood_pressure" in requested_metrics) + int(
            is_range and "body_composition" in requested_metrics
        )
        _require_calls((len(dates) * len(daily_metrics)) + range_calls)
        client = await ctx.get_state("client")

        # Collect metrics data
        metrics_data = []
        for date_str in dates:
            entry = {"date": ResponseBuilder.format_date_with_day(date_str)}

            # Steps
            if "steps" in requested_metrics:
                try:
                    steps = await client.call("get_steps_data", date_str)
                    entry["steps"] = steps
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["steps"] = None

            # Stress
            if "stress" in requested_metrics:
                try:
                    stress = await client.call("get_stress_data", date_str)
                    entry["stress"] = stress
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["stress"] = None

            # Respiration
            if "respiration" in requested_metrics:
                try:
                    respiration = await client.call("get_respiration_data", date_str)
                    entry["respiration"] = respiration
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["respiration"] = None

            # SpO2
            if "spo2" in requested_metrics:
                try:
                    spo2 = await client.call("get_spo2_data", date_str)
                    entry["spo2"] = spo2
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["spo2"] = None

            # Floors
            if "floors" in requested_metrics:
                try:
                    floors = await client.call("get_floors", date_str)
                    entry["floors"] = floors
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["floors"] = None

            # Hydration
            if "hydration" in requested_metrics:
                try:
                    hydration = await client.call("get_hydration_data", date_str)
                    entry["hydration"] = hydration
                except QueryBudgetError:
                    raise
                except Exception:
                    entry["hydration"] = None

            metrics_data.append(entry)
            if _stop_before_next_date_for_response_budget(
                items=metrics_data,
                data_factory=lambda values: {
                    "metrics": values,
                    "count": len(values),
                },
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                    "requested_metrics": requested_metrics,
                    "unit": unit,
                },
                pagination=pagination,
                surface="query_activity_metrics",
                page_position=page_position,
                page_filters=page_filters,
            ):
                break
        # Handle range-based metrics
        effective_dates = dates[: len(metrics_data)]
        if is_range and effective_dates:
            # Blood pressure (range only)
            if "blood_pressure" in requested_metrics:
                try:
                    bp = await client.call(
                        "get_blood_pressure",
                        effective_dates[0],
                        effective_dates[-1],
                    )
                    # Add to first entry or create separate field
                    if metrics_data:
                        metrics_data[0]["blood_pressure"] = bp
                except QueryBudgetError:
                    raise
                except Exception:
                    pass

            # Body composition (range only)
            if "body_composition" in requested_metrics:
                try:
                    bc = await client.call(
                        "get_body_composition",
                        effective_dates[0],
                        effective_dates[-1],
                    )
                    if metrics_data:
                        metrics_data[0]["body_composition"] = bc
                except QueryBudgetError:
                    raise
                except Exception:
                    pass

        # Generate insights
        insights = []
        insights.append(f"Requested metrics: {', '.join(requested_metrics)}")
        if len(metrics_data) == 1:
            available = [
                k for k in metrics_data[0].keys() if k != "date" and metrics_data[0][k] is not None
            ]
            if available:
                insights.append(f"Available metrics: {', '.join(available)}")
        else:
            insights.append(f"Metrics data for {len(metrics_data)} days")

        # Return appropriate structure
        if is_range:
            assert pagination is not None
            return ResponseBuilder.build_bounded_collection_response(
                items=metrics_data,
                data_factory=lambda values: {"metrics": values, "count": len(values)},
                analysis={"insights": insights} if insights else None,
                metadata_factory=lambda count: {
                    "start_date": dates[0] if dates else start_date,
                    "end_date": dates[count - 1] if count and dates else end_date,
                    "requested_metrics": requested_metrics,
                    "unit": unit,
                },
                pagination=pagination,
                cursor_factory=lambda count: encode_continuation_cursor(
                    surface="query_activity_metrics",
                    position=page_position + count,
                    page_size=pagination["limit"],
                    filters=page_filters,
                ),
                surface="query_activity_metrics",
            )
        else:
            budget = current_request_budget()
            if budget is not None:
                budget.reserve_items(len(metrics_data))
            return ResponseBuilder.build_response(
                data=metrics_data[0] if metrics_data else {},
                analysis={"insights": insights} if insights else None,
                metadata={
                    "date": dates[0] if dates else None,
                    "requested_metrics": requested_metrics,
                    "unit": unit,
                },
                surface="query_activity_metrics",
            )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)
