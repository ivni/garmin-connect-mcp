"""Bounded, capability-scoped health-data mutation tools."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder
from ..time_utils import local_noon_timestamp, parse_date_string


async def log_body_composition(
    data: Annotated[
        str,
        "JSON object with weight (20-500 kg) and optional body_fat/body_water percentages",
    ],
    date: Annotated[str | None, "Date (YYYY-MM-DD, defaults to today)"] = None,
    idempotency_key: Annotated[
        str | None,
        "Unique 8-128 character operation key; required when dry_run is false",
    ] = None,
    dry_run: Annotated[
        bool,
        "Validate and preview locally without contacting Garmin; set false to execute",
    ] = True,
    ctx: Context | None = None,
) -> str:
    """Preview or log one bounded body-composition entry."""
    try:
        params = _parse_json_object(data, {"weight", "body_fat", "body_water"})
        weight = _bounded_number(params, "weight", 20, 500, required=True)
        body_fat = _bounded_number(params, "body_fat", 0, 75)
        body_water = _bounded_number(params, "body_water", 0, 100)
        date_str = _entry_date(date)
        return await _execute_health_write(
            surface_name="log_body_composition",
            method_name="add_body_composition",
            capability="health.body_composition",
            date=date_str,
            params=params,
            method_args=(),
            method_kwargs={
                "timestamp": local_noon_timestamp(date_str),
                "weight": weight,
                "percent_fat": body_fat,
                "percent_hydration": body_water,
            },
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            ctx=ctx,
        )
    except ValueError as exc:
        return ResponseBuilder.build_error_response(str(exc), "invalid_parameters")
    except GarminAPIError as exc:
        return ResponseBuilder.build_exception_response(exc)
    except Exception as exc:
        return ResponseBuilder.build_exception_response(exc)


async def log_blood_pressure(
    data: Annotated[
        str,
        "JSON object with systolic (70-260), diastolic (40-150), and pulse (20-250)",
    ],
    date: Annotated[str | None, "Date (YYYY-MM-DD, defaults to today)"] = None,
    idempotency_key: Annotated[
        str | None,
        "Unique 8-128 character operation key; required when dry_run is false",
    ] = None,
    dry_run: Annotated[
        bool,
        "Validate and preview locally without contacting Garmin; set false to execute",
    ] = True,
    ctx: Context | None = None,
) -> str:
    """Preview or log one bounded blood-pressure entry."""
    try:
        params = _parse_json_object(data, {"systolic", "diastolic", "pulse"})
        systolic = _bounded_integer(params, "systolic", 70, 260)
        diastolic = _bounded_integer(params, "diastolic", 40, 150)
        pulse = _bounded_integer(params, "pulse", 20, 250)
        if diastolic >= systolic:
            raise ValueError("diastolic must be lower than systolic")
        date_str = _entry_date(date)
        return await _execute_health_write(
            surface_name="log_blood_pressure",
            method_name="set_blood_pressure",
            capability="health.blood_pressure",
            date=date_str,
            params=params,
            method_args=(systolic, diastolic, pulse, local_noon_timestamp(date_str)),
            method_kwargs={},
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            ctx=ctx,
        )
    except ValueError as exc:
        return ResponseBuilder.build_error_response(str(exc), "invalid_parameters")
    except GarminAPIError as exc:
        return ResponseBuilder.build_exception_response(exc)
    except Exception as exc:
        return ResponseBuilder.build_exception_response(exc)


async def log_hydration(
    data: Annotated[str, "JSON object with volume_ml from 1 through 5000"],
    date: Annotated[str | None, "Date (YYYY-MM-DD, defaults to today)"] = None,
    idempotency_key: Annotated[
        str | None,
        "Unique 8-128 character operation key; required when dry_run is false",
    ] = None,
    dry_run: Annotated[
        bool,
        "Validate and preview locally without contacting Garmin; set false to execute",
    ] = True,
    ctx: Context | None = None,
) -> str:
    """Preview or log one bounded positive hydration entry."""
    try:
        params = _parse_json_object(data, {"volume_ml"})
        volume_ml = _bounded_number(params, "volume_ml", 1, 5000, required=True)
        date_str = _entry_date(date)
        return await _execute_health_write(
            surface_name="log_hydration",
            method_name="add_hydration_data",
            capability="health.hydration",
            date=date_str,
            params=params,
            method_args=(volume_ml,),
            method_kwargs={
                "timestamp": local_noon_timestamp(date_str),
                "cdate": date_str,
            },
            idempotency_key=idempotency_key,
            dry_run=dry_run,
            ctx=ctx,
        )
    except ValueError as exc:
        return ResponseBuilder.build_error_response(str(exc), "invalid_parameters")
    except GarminAPIError as exc:
        return ResponseBuilder.build_exception_response(exc)
    except Exception as exc:
        return ResponseBuilder.build_exception_response(exc)


async def _execute_health_write(
    *,
    surface_name: str,
    method_name: str,
    capability: str,
    date: str,
    params: dict[str, object],
    method_args: tuple[object, ...],
    method_kwargs: dict[str, object],
    idempotency_key: str | None,
    dry_run: bool,
    ctx: Context | None,
) -> str:
    preview = {"date": date, "data": params}
    if dry_run:
        return ResponseBuilder.build_response(
            data={"preview": preview},
            analysis={"insights": ["Dry-run only; Garmin was not contacted"]},
            metadata={"dry_run": True, "capability": capability},
            surface=surface_name,
        )
    if idempotency_key is None:
        raise ValueError("idempotency_key is required when dry_run is false")
    assert ctx is not None
    client = await ctx.get_state("client")
    result = client.mutate(
        method_name,
        *method_args,
        **method_kwargs,
        idempotency_key=idempotency_key,
    )
    return ResponseBuilder.build_response(
        data={"result": result, **preview},
        analysis={"insights": [f"Health data submitted for {date}"]},
        metadata={
            "dry_run": False,
            "capability": capability,
            "idempotency_key": idempotency_key,
        },
        surface=surface_name,
    )


def _parse_json_object(value: str, allowed_fields: set[str]) -> dict[str, object]:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > 4096:
        raise ValueError("data must contain from 1 byte through 4096 bytes of JSON")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"data must be valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("data must be a JSON object")
    unknown = set(parsed) - allowed_fields
    if unknown:
        raise ValueError(f"unsupported data fields: {', '.join(sorted(unknown))}")
    return parsed


def _bounded_number(
    values: Mapping[str, object],
    name: str,
    minimum: float,
    maximum: float,
    *,
    required: bool = False,
) -> float | None:
    value = values.get(name)
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{name} must be from {minimum:g} through {maximum:g}")
    return result


def _bounded_integer(
    values: Mapping[str, object],
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be from {minimum} through {maximum}")
    return value


def _entry_date(value: str | None) -> str:
    return parse_date_string(value or "today").strftime("%Y-%m-%d")
