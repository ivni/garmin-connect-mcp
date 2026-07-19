"""Women's health tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..query_budget import (
    InvalidDateRangeError,
    QueryBudgetError,
    current_request_budget,
    policy_for_surface,
    reserve_projected_response_items,
    validate_date_range,
)
from ..response_builder import ResponseBuilder


async def query_womens_health(
    data_type: Annotated[str, "Data type: 'pregnancy' or 'menstrual'"],
    date: Annotated[str | None, "Specific date (YYYY-MM-DD)"] = None,
    start_date: Annotated[
        str | None, "Range start date (YYYY-MM-DD, for menstrual calendar)"
    ] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD, for menstrual calendar)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Query women's health data.

    Data types:
    - pregnancy: Get pregnancy tracking summary
    - menstrual: Get menstrual cycle data (for specific date or date range)
    """
    assert ctx is not None
    try:
        if data_type not in {"pregnancy", "menstrual"}:
            return ResponseBuilder.build_error_response(
                f"Invalid data type: {data_type}",
                "invalid_parameters",
                ["Valid types: 'pregnancy', 'menstrual'"],
            )
        if data_type == "pregnancy" and any(
            value is not None for value in (date, start_date, end_date)
        ):
            return ResponseBuilder.build_error_response(
                "Pregnancy summary does not accept date filters",
                "invalid_parameters",
            )
        if date is not None and (start_date is not None or end_date is not None):
            raise InvalidDateRangeError
        if (start_date is None) != (end_date is None):
            raise InvalidDateRangeError
        normalized_date = (
            validate_date_range(
                date,
                date,
                policy=policy_for_surface("query_womens_health"),
            ).start_iso
            if date is not None
            else None
        )
        bounded = (
            validate_date_range(
                start_date,
                end_date,
                policy=policy_for_surface("query_womens_health"),
            )
            if start_date is not None and end_date is not None
            else None
        )
        if data_type == "menstrual" and normalized_date is None and bounded is None:
            return ResponseBuilder.build_error_response(
                "Date or date range required for menstrual data",
                "invalid_parameters",
                [
                    "Provide date for single day",
                    "Or provide start_date and end_date for calendar view",
                ],
            )
        budget = current_request_budget()
        if budget is not None and (
            data_type == "pregnancy" or normalized_date is not None or bounded is not None
        ):
            budget.require_calls(1)
        client = await ctx.get_state("client")

        if data_type == "pregnancy":
            # Pregnancy summary
            summary = await client.call("get_pregnancy_summary")
            data = {"pregnancy_summary": summary}
            reserve_projected_response_items("query_womens_health", data)
            return ResponseBuilder.build_response(
                data=data,
                metadata={"data_type": "pregnancy"},
                surface="query_womens_health",
            )

        if data_type == "menstrual":
            # Menstrual data
            if normalized_date is not None:
                # Specific date
                menstrual_data = await client.call(
                    "get_menstrual_data_for_date",
                    normalized_date,
                )
                data = {"menstrual_data": menstrual_data, "date": normalized_date}
                reserve_projected_response_items("query_womens_health", data)
                return ResponseBuilder.build_response(
                    data=data,
                    metadata={
                        "data_type": "menstrual",
                        "query_type": "single_date",
                        "date": normalized_date,
                    },
                    surface="query_womens_health",
                )
            if bounded is not None:
                # Date range (calendar)
                calendar_data = await client.call(
                    "get_menstrual_calendar_data",
                    bounded.start_iso,
                    bounded.end_iso,
                )
                data = {"menstrual_calendar": calendar_data}
                reserve_projected_response_items("query_womens_health", data)
                return ResponseBuilder.build_response(
                    data=data,
                    metadata={
                        "data_type": "menstrual",
                        "query_type": "calendar",
                        "start_date": bounded.start_iso,
                        "end_date": bounded.end_iso,
                    },
                    surface="query_womens_health",
                )
            raise AssertionError("Validated menstrual mode was not dispatched")
        return ResponseBuilder.build_error_response(
            "Unsupported women's health query",
            "invalid_parameters",
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)
