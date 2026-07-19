"""Gear and equipment tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..query_budget import QueryBudgetError, reserve_projected_response_items
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
            gear = await client.call("get_gear", profile_number)
            data["gear"] = gear
        except QueryBudgetError:
            raise
        except Exception:
            data["gear"] = None

        # Gear defaults
        if include_defaults:
            try:
                defaults = await client.call("get_gear_defaults", profile_number)
                data["defaults"] = defaults
            except QueryBudgetError:
                raise
            except Exception:
                data["defaults"] = None

        # Gear stats
        if include_stats:
            try:
                stats = await client.call("get_gear_stats", gear_uuid)
                data["stats"] = stats
            except QueryBudgetError:
                raise
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

        reserve_projected_response_items("query_gear", data)
        return ResponseBuilder.build_response(
            data=data,
            analysis={"insights": insights} if insights else None,
            metadata={
                "gear_uuid": gear_uuid,
                "includes": {"defaults": include_defaults, "stats": include_stats},
            },
            surface="query_gear",
        )

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)
