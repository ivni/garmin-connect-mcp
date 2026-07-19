"""Gear and equipment tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder


async def query_gear(
    user_profile_number: Annotated[str, "Positive Garmin user profile number"],
    gear_uuid: Annotated[str | None, "Gear UUID required when include_stats is true"] = None,
    include_defaults: Annotated[bool, "Include default gear settings"] = True,
    include_stats: Annotated[bool, "Include usage statistics for gear_uuid"] = False,
    ctx: Context | None = None,
) -> str:
    """
    Query gear and equipment.

    Get comprehensive gear information including defaults and usage stats.
    """
    try:
        profile_number = str(int(user_profile_number))
    except (TypeError, ValueError):
        return ResponseBuilder.build_error_response(
            "user_profile_number must be a positive integer", "invalid_parameters"
        )
    if int(profile_number) <= 0:
        return ResponseBuilder.build_error_response(
            "user_profile_number must be a positive integer", "invalid_parameters"
        )
    if include_stats and not gear_uuid:
        return ResponseBuilder.build_error_response(
            "gear_uuid is required when include_stats is true", "invalid_parameters"
        )

    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        data = {}

        # Get all gear
        try:
            gear = client.safe_call("get_gear", profile_number)
            data["gear"] = gear
        except Exception:
            data["gear"] = None

        # Gear defaults
        if include_defaults:
            try:
                defaults = client.safe_call("get_gear_defaults", profile_number)
                data["defaults"] = defaults
            except Exception:
                data["defaults"] = None

        # Gear stats
        if include_stats:
            try:
                stats = client.safe_call("get_gear_stats", gear_uuid)
                data["stats"] = stats
            except Exception:
                data["stats"] = None

        # Generate insights
        insights = []
        if isinstance(data.get("gear"), list):
            insights.append(f"Total gear items: {len(data['gear'])}")
        if data.get("defaults"):
            insights.append("Default gear configured")
        if data.get("stats"):
            insights.append("Usage statistics available")

        return ResponseBuilder.build_response(
            data=data,
            analysis={"insights": insights} if insights else None,
            metadata={
                "user_profile_number": profile_number,
                "gear_uuid": gear_uuid,
                "includes": {"defaults": include_defaults, "stats": include_stats},
            },
        )

    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(
            e.message,
            "api_error",
            ["Check your Garmin Connect credentials", "Verify your internet connection"],
        )
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")
