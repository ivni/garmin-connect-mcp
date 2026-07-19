"""Weight management tools for Garmin Connect MCP server."""

import math
from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder
from ..time_utils import local_noon_timestamp, parse_date_string


async def query_weight_data(
    date: Annotated[str | None, "Specific date ('today', 'yesterday', or YYYY-MM-DD)"] = None,
    start_date: Annotated[str | None, "Range start date (YYYY-MM-DD)"] = None,
    end_date: Annotated[str | None, "Range end date (YYYY-MM-DD)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Query weight data.

    Get weight measurements for a specific date or date range.
    """
    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        # Determine query type
        if date:
            parsed_date = parse_date_string(date)
            date_str = parsed_date.strftime("%Y-%m-%d")
            weight_data = client.safe_call("get_daily_weigh_ins", date_str)
            return ResponseBuilder.build_response(
                data={"weigh_ins": weight_data, "date": date_str},
                metadata={"query_type": "single_date", "date": date_str},
            )
        elif start_date and end_date:
            weight_data = client.safe_call("get_weigh_ins", start_date, end_date)
            return ResponseBuilder.build_response(
                data={"weigh_ins": weight_data},
                metadata={"query_type": "range", "start_date": start_date, "end_date": end_date},
            )
        else:
            # Default to today
            date_str = parse_date_string("today").strftime("%Y-%m-%d")
            weight_data = client.safe_call("get_daily_weigh_ins", date_str)
            return ResponseBuilder.build_response(
                data={"weigh_ins": weight_data, "date": date_str},
                metadata={"query_type": "single_date", "date": date_str},
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(
            e.message, "api_error", ["Check your Garmin Connect credentials"]
        )
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")


async def add_weight_entry(
    weight: Annotated[float, "Weight in kg, from 20 through 500"],
    date: Annotated[str | None, "Date for entry (YYYY-MM-DD, defaults to today)"] = None,
    idempotency_key: Annotated[
        str | None,
        "Unique 8-128 character operation key; required when dry_run is false",
    ] = None,
    dry_run: Annotated[
        bool,
        "Preview locally without contacting Garmin; set false to execute",
    ] = True,
    ctx: Context | None = None,
) -> str:
    """Preview or add one bounded weight entry."""
    try:
        if not math.isfinite(weight) or not 20 <= weight <= 500:
            raise ValueError("weight must be a finite value from 20 through 500 kg")
        date_str = _entry_date(date)
        timestamp = local_noon_timestamp(date_str)
        preview = {"weight": weight, "date": date_str}
        if dry_run:
            return ResponseBuilder.build_response(
                data={"preview": preview},
                analysis={"insights": ["Dry-run only; Garmin was not contacted"]},
                metadata={"dry_run": True, "capability": "weight.write"},
            )
        _require_idempotency_key(idempotency_key)
        assert ctx is not None
        client = await ctx.get_state("client")
        result = client.mutate(
            "add_weigh_in",
            weight,
            "kg",
            timestamp,
            idempotency_key=idempotency_key,
        )
        return ResponseBuilder.build_response(
            data={"result": result, **preview},
            analysis={"insights": [f"Added weight entry: {weight} kg on {date_str}"]},
            metadata={
                "dry_run": False,
                "capability": "weight.write",
                "idempotency_key": idempotency_key,
            },
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), "invalid_parameters")
    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")


async def delete_weight_entries(
    date: Annotated[
        str | None,
        "Calendar date whose weigh-ins will all be deleted (defaults to today)",
    ] = None,
    confirmation: Annotated[
        str | None,
        "Exact confirmation returned by the dry-run preview",
    ] = None,
    idempotency_key: Annotated[
        str | None,
        "Unique 8-128 character operation key; required when dry_run is false",
    ] = None,
    dry_run: Annotated[
        bool,
        "Preview locally without contacting Garmin; set false to execute",
    ] = True,
    ctx: Context | None = None,
) -> str:
    """Preview or explicitly confirm deletion of all weigh-ins on one date."""
    try:
        date_str = _entry_date(date)
        required_confirmation = f"DELETE WEIGH-INS ON {date_str}"
        preview = {
            "date": date_str,
            "delete_all": True,
            "required_confirmation": required_confirmation,
        }
        if dry_run:
            return ResponseBuilder.build_response(
                data={"preview": preview},
                analysis={"insights": ["Dry-run only; Garmin was not contacted"]},
                metadata={"dry_run": True, "capability": "weight.delete"},
            )
        if confirmation != required_confirmation:
            raise ValueError(
                "confirmation must exactly match the required_confirmation from a fresh dry-run"
            )
        _require_idempotency_key(idempotency_key)
        assert ctx is not None
        client = await ctx.get_state("client")
        result = client.mutate(
            "delete_weigh_ins",
            date_str,
            True,
            idempotency_key=idempotency_key,
        )

        return ResponseBuilder.build_response(
            data={"result": result, "date": date_str},
            analysis={"insights": [f"Deleted all weigh-ins on {date_str}"]},
            metadata={
                "dry_run": False,
                "capability": "weight.delete",
                "idempotency_key": idempotency_key,
            },
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), "invalid_parameters")
    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")


def _entry_date(value: str | None) -> str:
    return parse_date_string(value or "today").strftime("%Y-%m-%d")


def _require_idempotency_key(value: str | None) -> None:
    if value is None:
        raise ValueError("idempotency_key is required when dry_run is false")
