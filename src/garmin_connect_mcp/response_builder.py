"""Response builder for structured Garmin Connect MCP responses."""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

import pydantic_core
from pydantic import BaseModel

from .pagination import PaginationInfo
from .types import JSONSerializable, UnitSystem


def _convert_datetimes(obj: Any) -> Any:  # type: ignore[misc]
    """Recursively convert datetime objects to ISO strings."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {str(k): _convert_datetimes(v) for k, v in obj.items()}  # type: ignore[misc]
    elif isinstance(obj, list):
        return [_convert_datetimes(item) for item in obj]  # type: ignore[misc]
    return obj


def _contains_key(obj: Any, key: str) -> bool:
    """Return whether a projected response tree contains a mapping key."""
    if isinstance(obj, Mapping):
        return key in obj or any(_contains_key(value, key) for value in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(value, key) for value in obj)
    return False


class ResponseBuilder:
    """Build structured responses with data, analysis, and metadata."""

    @staticmethod
    def format_date_with_day(dt: datetime | str | None) -> dict[str, str] | None:
        """Format a date/datetime with explicit day-of-week information.

        Args:
            dt: datetime object or ISO string or None

        Returns:
            Dict with datetime, date, day_of_week, and formatted string, or None if input is None

        Examples:
            >>> ResponseBuilder.format_date_with_day(datetime(2025, 10, 15, 14, 30))
            {
                "datetime": "2025-10-15T14:30:00",
                "date": "2025-10-15",
                "day_of_week": "Wednesday",
                "formatted": "Wednesday, October 15, 2025 at 02:30 PM"
            }
        """
        if dt is None:
            return None

        # Parse the datetime if it's a string, otherwise use it directly
        if isinstance(dt, str):
            parsed_dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        else:
            parsed_dt = dt

        return {
            "datetime": dt if isinstance(dt, str) else dt.isoformat(),
            "date": parsed_dt.strftime("%Y-%m-%d"),
            "day_of_week": parsed_dt.strftime("%A"),  # e.g., "Monday"
            "formatted": parsed_dt.strftime(
                "%A, %B %d, %Y at %I:%M %p"
            ),  # e.g., "Monday, October 15, 2025 at 02:30 PM"
        }

    @staticmethod
    def serialized_envelope_size(response: str, surface: str | None) -> int:
        """Measure the actual FastMCP result envelope for a public response."""
        if surface is not None and surface.startswith("garmin://"):
            from fastmcp.resources import ResourceResult

            envelope: BaseModel = ResourceResult(response).to_mcp_result(surface)
        else:
            from fastmcp.tools.base import ToolResult

            internal_result = ToolResult(
                content=response,
                structured_content={"result": response},
                meta={"fastmcp": {"wrap_result": True}},
            )
            envelope = cast(BaseModel, internal_result.to_mcp_result())
        return len(envelope.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))

    @staticmethod
    def serialized_result_size(result: Any, surface: str) -> int:
        """Measure the MCP wire result produced by a FastMCP middleware boundary."""
        from fastmcp.resources import ResourceResult
        from fastmcp.tools.base import ToolResult
        from mcp.types import CallToolResult

        if isinstance(result, ToolResult):
            converted = result.to_mcp_result()
            if isinstance(converted, BaseModel):
                envelope = converted
            elif isinstance(converted, tuple):
                envelope = CallToolResult(
                    content=converted[0],
                    structuredContent=converted[1],
                )
            else:
                envelope = CallToolResult(content=converted)
        elif isinstance(result, ResourceResult):
            envelope = result.to_mcp_result(surface)
        elif isinstance(result, str):
            return ResponseBuilder.serialized_envelope_size(result, surface)
        elif isinstance(result, BaseModel):
            envelope = result
        else:
            return len(pydantic_core.to_json(result, fallback=str))
        return len(envelope.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))

    @staticmethod
    def serialized_tool_error_size(response: str) -> int:
        """Measure the exact MCP error envelope produced for ``ToolError`` text."""
        from mcp.types import CallToolResult, TextContent

        envelope = CallToolResult(
            content=[TextContent(type="text", text=response)],
            isError=True,
        )
        return len(envelope.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8"))

    @staticmethod
    def result_contains_error(result: Any) -> bool:
        """Recognize the stable top-level error envelope after a confirmed write."""
        candidates: list[Any] = [result]
        try:
            from fastmcp.tools.base import ToolResult

            if isinstance(result, ToolResult):
                if result.structured_content is not None:
                    candidates.append(result.structured_content.get("result"))
                candidates.extend(getattr(item, "text", None) for item in result.content)
        except ImportError:  # pragma: no cover - FastMCP is a runtime dependency
            pass
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, Mapping) and isinstance(parsed.get("error"), Mapping):
                return True
        return False

    @staticmethod
    def build_response(
        data: JSONSerializable,
        analysis: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        pagination: PaginationInfo | dict[str, Any] | None = None,
        *,
        surface: str | None = None,
        policy_context: Mapping[str, object] | None = None,
        _enforce_budget: bool = True,
    ) -> str:
        """
        Build a structured response with data, optional analysis, and metadata.

        Args:
            data: The primary data payload
            analysis: Optional analysis and insights about the data
            metadata: Optional metadata about the query/response
            pagination: Optional pagination metadata

        Returns:
            JSON string with structured response
        """
        public_data: Any = data
        if surface is not None:
            from .response_policy import project_surface_data

            public_data = project_surface_data(surface, data, policy_context)

        # Convert datetime objects to ISO strings
        converted_data = cast(JSONSerializable, _convert_datetimes(public_data))
        converted_analysis: dict[str, Any] | None = None
        if analysis:
            converted_analysis = cast(dict[str, Any], _convert_datetimes(analysis))

        response: dict[str, Any] = {"data": converted_data}

        if converted_analysis:
            response["analysis"] = converted_analysis

        if pagination:
            response["pagination"] = pagination

        # Build metadata with timestamp
        meta = dict(metadata or {})
        converted_meta = cast(dict[str, Any], _convert_datetimes(meta))
        converted_meta["response_schema"] = "2"
        if (
            surface in {"query_activities", "get_activity_details"}
            and policy_context
            and policy_context.get("include_location") is True
            and _contains_key(public_data, "location")
        ):
            converted_meta["precise_location"] = True
        converted_meta["fetched_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        response["metadata"] = converted_meta

        serialized = json.dumps(response, separators=(",", ":"))
        from .query_budget import ResponseBudgetExceededError, current_request_budget

        budget = current_request_budget()
        if budget is not None and _enforce_budget:
            byte_count = ResponseBuilder.serialized_envelope_size(serialized, surface)
            if byte_count > budget.policy.max_response_bytes and budget.mutation_confirmed:
                return ResponseBuilder.build_confirmed_mutation_budget_response(surface)
            try:
                budget.record_response_size(byte_count)
            except ResponseBudgetExceededError as error:
                return ResponseBuilder.build_budget_error_response(error)
        return serialized

    @staticmethod
    def build_confirmed_mutation_budget_response(
        surface: str | None,
        *,
        omitted_reason: str = "response_budget",
    ) -> str:
        """Return a bounded success when post-write delivery cannot be trusted."""
        response = ResponseBuilder.build_response(
            data={"result": {"success": True}},
            analysis={
                "insights": [
                    "The Garmin mutation was confirmed; its verbose result was omitted "
                    "so a later response failure cannot encourage an unsafe retry"
                ]
            },
            metadata={
                "mutation_confirmed": True,
                "result_omitted": omitted_reason,
            },
            surface=surface,
            _enforce_budget=False,
        )
        from .query_budget import current_request_budget

        budget = current_request_budget()
        if budget is not None:
            budget.note_outcome("confirmed")
            budget.record_response_size(ResponseBuilder.serialized_envelope_size(response, surface))
        return response

    @staticmethod
    def build_bounded_collection_response(
        *,
        items: list[Any],
        data_factory: Callable[[list[Any]], JSONSerializable],
        metadata_factory: Callable[[int], dict[str, Any]],
        pagination: PaginationInfo,
        cursor_factory: Callable[[int], str],
        surface: str,
        analysis: dict[str, Any] | None = None,
        policy_context: Mapping[str, object] | None = None,
    ) -> str:
        """Serialize the largest whole-item prefix that fits the request budget."""
        from .query_budget import (
            ResponseBudgetExceededError,
            current_request_budget,
        )

        def render(count: int, page: PaginationInfo) -> str:
            return ResponseBuilder.build_response(
                data=data_factory(items[:count]),
                analysis=analysis if count == len(items) else None,
                metadata=metadata_factory(count),
                pagination=page,
                surface=surface,
                policy_context=policy_context,
                _enforce_budget=False,
            )

        full_response = render(len(items), pagination)
        budget = current_request_budget()
        if budget is None:
            return full_response

        full_size = ResponseBuilder.serialized_envelope_size(full_response, surface)
        if full_size <= budget.policy.max_response_bytes:
            budget.reserve_items(len(items))
            budget.record_response_size(full_size)
            return full_response

        if not budget.policy.partial_results_allowed:
            try:
                budget.record_response_size(full_size)
            except ResponseBudgetExceededError as error:
                return ResponseBuilder.build_budget_error_response(error)
            raise AssertionError("Oversized response was accepted by its budget")

        best_response: str | None = None
        best_count = 0
        for count in range(1, len(items)):
            bounded_page: PaginationInfo = {
                "cursor": cursor_factory(count),
                "has_more": True,
                "limit": pagination["limit"],
                "returned": count,
                "partial": True,
                "truncation_reason": "response_bytes",
            }
            candidate = render(count, bounded_page)
            if (
                ResponseBuilder.serialized_envelope_size(candidate, surface)
                > budget.policy.max_response_bytes
            ):
                break
            best_count = count
            best_response = candidate

        if best_response is None:
            try:
                budget.record_response_size(full_size)
            except ResponseBudgetExceededError as error:
                return ResponseBuilder.build_budget_error_response(error)
            raise AssertionError("Oversized response was accepted by its budget")

        budget.reserve_items(best_count)
        budget.record_response_size(
            ResponseBuilder.serialized_envelope_size(best_response, surface)
        )
        budget.note_truncation("response_bytes")
        return best_response

    @staticmethod
    def preview_response_size(
        *,
        data: JSONSerializable,
        metadata: dict[str, Any] | None = None,
        pagination: PaginationInfo | dict[str, Any] | None = None,
        surface: str | None = None,
        policy_context: Mapping[str, object] | None = None,
    ) -> int:
        """Measure the exact projected serialized size without charging the budget."""
        response = ResponseBuilder.build_response(
            data=data,
            metadata=metadata,
            pagination=pagination,
            surface=surface,
            policy_context=policy_context,
            _enforce_budget=False,
        )
        return ResponseBuilder.serialized_envelope_size(response, surface)

    @staticmethod
    def build_error_response(
        message: str,
        error_type: str = "error",
        suggestions: list[str] | None = None,
        code: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """
        Build a structured error response.

        Args:
            message: Error message
            error_type: Type of error (error, warning, etc.)
            suggestions: Optional list of suggestions to resolve the error

        Returns:
            JSON string with error response
        """
        from .response_policy.errors import default_error_code

        resolved_code = code or default_error_code(error_type)
        response: dict[str, Any] = {
            "error": {
                "code": resolved_code,
                "type": error_type,
                "message": message,
                "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            "metadata": {"response_schema": "2"},
        }

        if request_id:
            response["error"]["request_id"] = request_id

        if suggestions:
            response["error"]["suggestions"] = suggestions

        from .query_budget import current_request_budget

        budget = current_request_budget()
        if budget is not None:
            budget.note_outcome(resolved_code)
        return json.dumps(response, separators=(",", ":"))

    @staticmethod
    def build_exception_response(error: Exception) -> str:
        """Build a stable public error without exposing ``str(error)``."""
        from .response_policy.errors import public_error_for_exception

        public = public_error_for_exception(error)
        from .query_budget import current_request_budget

        budget = current_request_budget()
        if budget is not None:
            budget.note_outcome(public.code)
        return ResponseBuilder.build_error_response(
            public.message,
            public.error_type,
            code=public.code,
            request_id=public.request_id,
        )

    @staticmethod
    def build_budget_error_response(error: Exception) -> str:
        """Build a stable budget failure without exposing internal values."""
        from .query_budget import QueryBudgetError

        if not isinstance(error, QueryBudgetError):
            return ResponseBuilder.build_exception_response(error)
        from .query_budget import current_request_budget

        budget = current_request_budget()
        if budget is not None:
            budget.note_outcome(error.public_code)
            if error.public_code == "RESPONSE_BUDGET_EXCEEDED":
                budget.note_truncation("response_bytes")
        if error.public_code in {
            "AUTH_REQUIRED",
            "RATE_LIMITED",
            "GARMIN_UPSTREAM_UNAVAILABLE",
            "CAPABILITY_UNAVAILABLE",
        }:
            return ResponseBuilder.build_exception_response(error)
        return ResponseBuilder.build_error_response(
            error.public_message,
            error.error_type,
            code=error.public_code,
        )

    @staticmethod
    def format_activity(
        activity_dict: dict[str, Any], unit: UnitSystem = "metric"
    ) -> dict[str, Any]:
        """
        Format an activity with rich formatting (raw + human-readable).

        Args:
            activity_dict: Raw activity data
            unit: Unit system ('metric' or 'imperial')

        Returns:
            Formatted activity dictionary with enhanced fields
        """
        formatted = activity_dict.copy()

        # Format distance
        if "distance" in activity_dict and activity_dict["distance"] is not None:
            meters = activity_dict["distance"]
            formatted["distance"] = {
                "meters": meters,
                "formatted": ResponseBuilder._format_distance(meters, unit),
            }

        # Format duration
        if "duration" in activity_dict and activity_dict["duration"] is not None:
            seconds = activity_dict["duration"]
            formatted["duration"] = {
                "seconds": seconds,
                "formatted": ResponseBuilder._format_duration(seconds),
            }

        # Format elevation
        if "elevationGain" in activity_dict and activity_dict["elevationGain"] is not None:
            meters = activity_dict["elevationGain"]
            formatted["elevationGain"] = {
                "meters": meters,
                "formatted": ResponseBuilder._format_elevation(meters, unit),
            }

        # Format pace/speed
        if "averageSpeed" in activity_dict and activity_dict["averageSpeed"] is not None:
            mps = activity_dict["averageSpeed"]
            formatted["averageSpeed"] = {
                "mps": mps,
                "formatted_speed": ResponseBuilder._format_speed(mps, unit),
                "formatted_pace": ResponseBuilder._format_pace(mps, unit),
            }

        # Format dates with day-of-week information
        for date_field in ["startTimeLocal", "startTimeGMT", "endTimeLocal"]:
            if date_field in activity_dict and activity_dict[date_field]:
                formatted[date_field] = ResponseBuilder.format_date_with_day(
                    activity_dict[date_field]
                )

        # Format heart rate
        if "averageHR" in activity_dict and activity_dict["averageHR"] is not None:
            formatted["heart_rate"] = {"avg_bpm": round(activity_dict["averageHR"])}

        if "maxHR" in activity_dict and activity_dict["maxHR"] is not None:
            if "heart_rate" not in formatted:
                formatted["heart_rate"] = {}
            formatted["heart_rate"]["max_bpm"] = round(activity_dict["maxHR"])

        # Format power
        if "avgPower" in activity_dict and activity_dict["avgPower"] is not None:
            formatted["power"] = {"avg_watts": round(activity_dict["avgPower"])}

        if "maxPower" in activity_dict and activity_dict["maxPower"] is not None:
            if "power" not in formatted:
                formatted["power"] = {}
            formatted["power"]["max_watts"] = round(activity_dict["maxPower"])

        # Format cadence
        if "avgRunCadence" in activity_dict and activity_dict["avgRunCadence"] is not None:
            formatted["cadence"] = {"avg_spm": round(activity_dict["avgRunCadence"])}
        elif (
            "averageBikingCadenceInRevPerMinute" in activity_dict
            and activity_dict["averageBikingCadenceInRevPerMinute"] is not None
        ):
            formatted["cadence"] = {
                "avg_rpm": round(activity_dict["averageBikingCadenceInRevPerMinute"])
            }

        # Format calories
        if "calories" in activity_dict and activity_dict["calories"] is not None:
            formatted["calories"] = activity_dict["calories"]

        return formatted

    @staticmethod
    def format_health_metric(
        metric_dict: dict[str, Any], unit: UnitSystem = "metric"
    ) -> dict[str, Any]:
        """
        Format a health metric with rich formatting.

        Args:
            metric_dict: Raw health metric data
            unit: Unit system ('metric' or 'imperial')

        Returns:
            Formatted metric dictionary
        """
        formatted = metric_dict.copy()

        # Format weight
        if "weight" in metric_dict and metric_dict["weight"] is not None:
            grams = metric_dict["weight"]
            formatted["weight"] = {
                "grams": grams,
                "formatted": ResponseBuilder._format_weight(grams, unit),
            }

        # Format steps
        if "steps" in metric_dict and metric_dict["steps"] is not None:
            steps = metric_dict["steps"]
            formatted["steps"] = {
                "value": steps,
                "formatted": f"{steps:,}",
            }

        # Format heart rate
        if "heartRate" in metric_dict and metric_dict["heartRate"] is not None:
            hr = metric_dict["heartRate"]
            formatted["heartRate"] = {
                "bpm": hr,
                "formatted": f"{hr} bpm",
            }

        return formatted

    # Helper formatting methods
    @staticmethod
    def _format_distance(meters: float, unit: UnitSystem = "metric") -> str:
        """Format distance with units."""
        if unit == "imperial":
            miles = meters / 1609.34
            return f"{miles:.2f} mi"
        else:
            km = meters / 1000
            return f"{km:.2f} km"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Format duration in seconds to human-readable format."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)

        if hours > 0:
            return f"{hours}h {minutes}m {secs}s"
        elif minutes > 0:
            return f"{minutes}m {secs}s"
        else:
            return f"{secs}s"

    @staticmethod
    def _format_elevation(meters: float, unit: UnitSystem = "metric") -> str:
        """Format elevation with units."""
        if unit == "imperial":
            feet = meters * 3.28084
            return f"{feet:.0f} ft"
        else:
            return f"{meters:.0f} m"

    @staticmethod
    def _format_speed(mps: float, unit: UnitSystem = "metric") -> str:
        """Format speed from m/s."""
        if unit == "imperial":
            mph = mps * 2.23694
            return f"{mph:.2f} mph"
        else:
            kmh = mps * 3.6
            return f"{kmh:.2f} km/h"

    @staticmethod
    def _format_pace(mps: float, unit: UnitSystem = "metric") -> str:
        """Format pace from m/s."""
        if mps == 0:
            return "N/A"

        if unit == "imperial":
            # min/mile
            seconds_per_mile = 1609.34 / mps
            minutes = int(seconds_per_mile // 60)
            seconds = int(seconds_per_mile % 60)
            return f"{minutes}:{seconds:02d} /mi"
        else:
            # min/km
            seconds_per_km = 1000 / mps
            minutes = int(seconds_per_km // 60)
            seconds = int(seconds_per_km % 60)
            return f"{minutes}:{seconds:02d} /km"

    @staticmethod
    def _format_weight(grams: float, unit: UnitSystem = "metric") -> str:
        """Format weight with units."""
        if unit == "imperial":
            lbs = grams / 453.592
            return f"{lbs:.2f} lbs"
        else:
            kg = grams / 1000
            return f"{kg:.2f} kg"

    @staticmethod
    def _format_datetime(date_str: str | datetime | None) -> str:
        """Format a datetime string or datetime object."""
        if date_str is None:
            return "N/A"

        if isinstance(date_str, str):
            try:
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return date_str

        if isinstance(date_str, datetime):
            return date_str.strftime("%Y-%m-%d %H:%M:%S")

        return str(date_str)

    @staticmethod
    def aggregate_activities(
        activities: list[dict[str, Any]], unit: UnitSystem = "metric"
    ) -> dict[str, Any]:
        """Aggregate metrics across multiple activities.

        Args:
            activities: List of activity data dictionaries
            unit: Unit preference for formatting

        Returns:
            Dict with aggregated metrics (totals, averages, counts)
        """
        if not activities:
            return {}

        total_distance = sum(a.get("distance", 0) for a in activities)
        total_time = sum(a.get("duration", 0) for a in activities)
        total_elevation = sum(a.get("elevationGain", 0) for a in activities)
        total_calories = sum(a.get("calories", 0) for a in activities)

        aggregated: dict[str, Any] = {
            "count": len(activities),
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
        }

        if total_calories > 0:
            aggregated["total_calories"] = total_calories

        # Average distance per activity
        if len(activities) > 0:
            avg_distance = total_distance / len(activities)
            aggregated["avg_distance_per_activity"] = {
                "meters": avg_distance,
                "formatted": ResponseBuilder._format_distance(avg_distance, unit),
            }

        # Average pace/speed (if applicable)
        if total_time > 0 and total_distance > 0:
            avg_speed = total_distance / total_time
            aggregated["avg_speed"] = {
                "mps": avg_speed,
                "formatted_speed": ResponseBuilder._format_speed(avg_speed, unit),
                "formatted_pace": ResponseBuilder._format_pace(avg_speed, unit),
            }

        return aggregated
