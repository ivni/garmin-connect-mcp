"""Device management tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
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
        solar_start = parse_date_string(solar_start_date or "today").strftime("%Y-%m-%d")
        solar_end = parse_date_string(solar_end_date or solar_start).strftime("%Y-%m-%d")
    except ValueError as exc:
        return ResponseBuilder.build_error_response(str(exc), "invalid_parameters")
    if solar_start > solar_end:
        return ResponseBuilder.build_error_response(
            "solar_start_date must be before or equal to solar_end_date",
            "invalid_parameters",
        )

    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        data = {}

        # Get all devices
        try:
            devices = client.safe_call("get_devices")
            data["devices"] = devices
        except Exception:
            data["devices"] = None

        # Last used device
        if include_last_used:
            try:
                last_used = client.safe_call("get_device_last_used")
                data["last_used"] = last_used
            except Exception:
                data["last_used"] = None

        # Primary training device
        if include_primary:
            try:
                primary = client.safe_call("get_primary_training_device")
                data["primary_device"] = primary
            except Exception:
                data["primary_device"] = None

        # Device-specific details
        if device_id is not None:
            if include_settings:
                try:
                    settings = client.safe_call("get_device_settings", device_id)
                    data["device_settings"] = settings
                except Exception:
                    data["device_settings"] = None

            if include_solar_data:
                try:
                    solar = client.safe_call(
                        "get_device_solar_data", device_id, solar_start, solar_end
                    )
                    data["solar_data"] = solar
                except Exception:
                    data["solar_data"] = None

        if include_alarms:
            try:
                alarms = client.safe_call("get_device_alarms")
                data["alarms"] = alarms
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
