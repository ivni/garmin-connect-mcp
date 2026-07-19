"""Weight management tools for Garmin Connect MCP server."""

import math
from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder
from ..time_utils import parse_date_string

MAX_WEIGH_IN_ID = 2**63 - 1


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
            date_str,
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
    weigh_in_ids: Annotated[
        str,
        "Comma-separated positive 64-bit weigh-in IDs; maximum 25",
    ],
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
    """Preview or explicitly confirm deletion of bounded weigh-in IDs."""
    try:
        ids = _parse_weigh_in_ids(weigh_in_ids)
        required_confirmation = f"DELETE WEIGH-INS {','.join(str(value) for value in ids)}"
        preview = {
            "weigh_in_ids": ids,
            "count": len(ids),
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
            ids,
            idempotency_key=idempotency_key,
        )

        return ResponseBuilder.build_response(
            data={"result": result, "deleted_ids": ids},
            analysis={"insights": [f"Deleted {len(ids)} weight entries"]},
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


def _parse_weigh_in_ids(value: str) -> list[int]:
    if not value or len(value) > 512:
        raise ValueError("weigh_in_ids must be a non-empty comma-separated value")
    try:
        parsed = [int(part.strip()) for part in value.split(",")]
    except ValueError as exc:
        raise ValueError("weigh_in_ids must contain only integers") from exc
    if not 1 <= len(parsed) <= 25:
        raise ValueError("weigh_in_ids must contain from 1 through 25 IDs")
    if any(value <= 0 or value > MAX_WEIGH_IN_ID for value in parsed):
        raise ValueError(f"weigh_in_ids must be from 1 through {MAX_WEIGH_IN_ID}")
    if len(set(parsed)) != len(parsed):
        raise ValueError("weigh_in_ids must not contain duplicates")
    return parsed
