"""Device management tools for Garmin Connect MCP server."""

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
from ..time_utils import parse_date_string


async def query_devices(
    device_id: Annotated[int | None, "Specific device ID"] = None,
    include_last_used: Annotated[bool, "Include last used device info"] = True,
    include_primary: Annotated[bool, "Include primary training device"] = True,
    include_settings: Annotated[bool, "Include device settings"] = False,
    include_solar_data: Annotated[bool, "Include solar charging data"] = False,
    solar_start_date: Annotated[
        str | None, "Solar range start ('today', 'yesterday', or YYYY-MM-DD)"
    ] = None,
    solar_end_date: Annotated[str | None, "Solar range end (YYYY-MM-DD)"] = None,
    include_alarms: Annotated[bool, "Include device alarms"] = False,
    ctx: Context | None = None,
) -> str:
    """
    Query Garmin devices.

    Get comprehensive device information including last used device,
    primary training device, settings, solar data, and alarms.
    """
    if device_id is not None and device_id <= 0:
        return ResponseBuilder.build_error_response(
            "device_id must be positive", "invalid_parameters"
        )
    if include_solar_data and device_id is None:
        return ResponseBuilder.build_error_response(
            "device_id is required when include_solar_data is true",
            "invalid_parameters",
        )

    try:
        if not include_solar_data and (solar_start_date is not None or solar_end_date is not None):
            raise InvalidDateRangeError
        if include_solar_data:
            normalized_start = parse_date_string(solar_start_date or "today").strftime("%Y-%m-%d")
            bounded = validate_date_range(
                normalized_start,
                solar_end_date or normalized_start,
                policy=policy_for_surface("query_devices"),
            )
            solar_start = bounded.start_iso
            solar_end = bounded.end_iso
        else:
            solar_start = None
            solar_end = None
    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except ValueError:
        return ResponseBuilder.build_budget_error_response(InvalidDateRangeError())

    assert ctx is not None
    try:
        required_calls = (
            1
            + int(include_last_used)
            + int(include_primary)
            + int(device_id is not None and include_settings)
            + int(device_id is not None and include_solar_data)
            + int(include_alarms)
        )
        budget = current_request_budget()
        if budget is not None:
            budget.require_calls(required_calls)
        client = await ctx.get_state("client")

        data = {}

        # Get all devices
        try:
            devices = await client.call("get_devices")
            data["devices"] = devices
        except QueryBudgetError:
            raise
        except Exception:
            data["devices"] = None

        # Last used device
        if include_last_used:
            try:
                last_used = await client.call("get_device_last_used")
                data["last_used"] = last_used
            except QueryBudgetError:
                raise
            except Exception:
                data["last_used"] = None

        # Primary training device
        if include_primary:
            try:
                primary = await client.call("get_primary_training_device")
                data["primary_device"] = primary
            except QueryBudgetError:
                raise
            except Exception:
                data["primary_device"] = None

        # Device-specific details
        if device_id is not None:
            if include_settings:
                try:
                    settings = await client.call("get_device_settings", device_id)
                    data["device_settings"] = settings
                except QueryBudgetError:
                    raise
                except Exception:
                    data["device_settings"] = None

            if include_solar_data:
                try:
                    solar = await client.call(
                        "get_device_solar_data", device_id, solar_start, solar_end
                    )
                    data["solar_data"] = solar
                except QueryBudgetError:
                    raise
                except Exception:
                    data["solar_data"] = None

        if include_alarms:
            try:
                alarms = await client.call("get_device_alarms")
                data["alarms"] = alarms
            except QueryBudgetError:
                raise
            except Exception:
                data["alarms"] = None

        # Generate insights
        insights = []
        if isinstance(data.get("devices"), list):
            insights.append(f"Total devices: {len(data['devices'])}")
        if data.get("primary_device"):
            insights.append("Primary training device identified")
        if data.get("solar_data"):
            insights.append("Solar charging data available")

        reserve_projected_response_items("query_devices", data)

        return ResponseBuilder.build_response(
            data=data,
            analysis={"insights": insights} if insights else None,
            metadata={
                "device_id": device_id,
                "solar_start_date": solar_start if include_solar_data else None,
                "solar_end_date": solar_end if include_solar_data else None,
            },
            surface="query_devices",
        )

    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)
