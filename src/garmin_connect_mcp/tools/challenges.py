"""Challenges, goals, and records tools for Garmin Connect MCP server."""

from typing import Annotated, Any

from fastmcp import Context

from ..client import GarminAPIError
from ..pagination import (
    decode_continuation_cursor,
    encode_continuation_cursor,
    validate_continuation_headroom,
)
from ..query_budget import (
    InvalidContinuationCursorError,
    InvalidPageSizeError,
    QueryBudgetError,
    current_request_budget,
    policy_for_surface,
    reserve_projected_response_items,
)
from ..response_builder import ResponseBuilder

CHALLENGE_ITEM_KEYS = frozenset(
    {
        "name",
        "displayName",
        "description",
        "status",
        "progress",
        "points",
        "startDate",
        "endDate",
        "typeId",
        "typeKey",
        "targetValue",
        "currentValue",
    }
)


async def query_goals_and_records(
    include_goals: Annotated[bool, "Include activity goals"] = True,
    include_prs: Annotated[bool, "Include personal records"] = True,
    include_race_predictions: Annotated[bool, "Include race time predictions"] = True,
    ctx: Context | None = None,
) -> str:
    """Get bounded personal records and race predictions.

    Goals are reported as unavailable because garminconnect 0.3.6 implements
    ``get_goals`` as an internal unbounded auto-pagination loop.
    """
    assert ctx is not None
    try:
        client = await ctx.get_state("client")
        data: dict[str, Any] = {}
        unavailable: list[str] = []

        if include_goals:
            data["goals"] = None
            unavailable.append(
                "Goals are unavailable because the supported dependency has no single-page API"
            )

        if include_prs:
            try:
                data["personal_records"] = await client.call("get_personal_record")
            except QueryBudgetError:
                raise
            except Exception:
                data["personal_records"] = None

        if include_race_predictions:
            try:
                data["race_predictions"] = await client.call("get_race_predictions")
            except QueryBudgetError:
                raise
            except Exception:
                data["race_predictions"] = None

        available = [key for key, value in data.items() if value is not None]
        insights = [f"Available data: {', '.join(available)}"] if available else []
        insights.extend(unavailable)
        if not insights:
            insights.append("No goals, PRs, or predictions data requested")

        reserve_projected_response_items("query_goals_and_records", data)
        return ResponseBuilder.build_response(
            data=data,
            analysis={"insights": insights},
            metadata={
                "includes": {
                    "goals": include_goals,
                    "prs": include_prs,
                    "race_predictions": include_race_predictions,
                },
                "unavailable": ["goals"] if include_goals else [],
            },
            surface="query_goals_and_records",
        )
    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as exc:
        return ResponseBuilder.build_exception_response(exc)
    except Exception as exc:
        return ResponseBuilder.build_exception_response(exc)


def _bounded_challenge_page_size(limit: str | int | None) -> int:
    maximum = policy_for_surface("query_challenges").max_page_items
    if limit is None:
        return maximum
    if isinstance(limit, bool):
        raise InvalidPageSizeError(maximum)
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise InvalidPageSizeError(maximum) from exc
    if not 1 <= value <= maximum:
        raise InvalidPageSizeError(maximum)
    return value


def _challenge_items(value: Any) -> list[Any]:
    """Flatten dependency page wrappers into the canonical public item list."""
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    if CHALLENGE_ITEM_KEYS.intersection(value):
        return [value]
    items: list[Any] = []
    for nested in value.values():
        if isinstance(nested, list | dict):
            items.extend(_challenge_items(nested))
    return items


def _normalize_challenge_page(
    value: Any,
    limit: int,
    position: int = 0,
) -> tuple[list[Any], int, bool]:
    """Keep at most ``limit`` actual items across one category response."""
    items = _challenge_items(value)
    total_count = value.get("totalCount") if isinstance(value, dict) else None
    known_remaining = (
        isinstance(total_count, int)
        and not isinstance(total_count, bool)
        and total_count > position + len(items)
    )
    has_more = len(items) > limit or known_remaining
    if known_remaining and len(items) < limit:
        raise GarminAPIError(
            "Garmin returned a short challenge page that cannot be continued safely."
        )
    return items[:limit], min(len(items), limit), has_more


async def query_challenges(
    status: Annotated[str, "Challenge status: 'active', 'available', 'earned', 'all'"] = "active",
    challenge_type: Annotated[str, "Challenge type: 'badge', 'adhoc', 'virtual', 'all'"] = "all",
    cursor: Annotated[str | None, "Continuation cursor for the same category filters"] = None,
    limit: Annotated[int | None, "Per-category page size (1-50)"] = None,
    ctx: Context | None = None,
) -> str:
    """Query synchronized bounded pages of challenge categories."""
    if status not in {"active", "available", "earned", "all"}:
        return ResponseBuilder.build_error_response(
            f"Invalid challenge status: {status}",
            "invalid_parameters",
            ["Valid statuses: 'active', 'available', 'earned', 'all'"],
        )
    if challenge_type not in {"badge", "adhoc", "virtual", "all"}:
        return ResponseBuilder.build_error_response(
            f"Invalid challenge type: {challenge_type}",
            "invalid_parameters",
            ["Valid types: 'badge', 'adhoc', 'virtual', 'all'"],
        )

    surface = "query_challenges"
    filters = {"status": status, "challenge_type": challenge_type}
    policy = policy_for_surface(surface)
    try:
        if cursor is None:
            position = 0
            page_size = _bounded_challenge_page_size(limit)
        else:
            position, page_size = decode_continuation_cursor(
                cursor,
                surface=surface,
                filters=filters,
                policy=policy,
            )
            if page_size > policy.max_page_items:
                raise InvalidContinuationCursorError
            if limit is not None and _bounded_challenge_page_size(limit) != page_size:
                raise InvalidContinuationCursorError
    except ValueError:
        return ResponseBuilder.build_budget_error_response(InvalidContinuationCursorError())
    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)

    assert ctx is not None
    try:
        client = await ctx.get_state("client")
        calls: list[tuple[str, str, int]] = []
        if challenge_type in {"badge", "all"}:
            if status in {"available", "all"}:
                calls.append(("available_badges", "get_available_badge_challenges", position))
            if status in {"active", "all"}:
                calls.append(("active_badges", "get_non_completed_badge_challenges", position))
            if status in {"earned", "all"}:
                calls.append(("all_badge_challenges", "get_badge_challenges", position))
        if challenge_type in {"adhoc", "all"} and status == "all":
            calls.append(("adhoc_challenges", "get_adhoc_challenges", position))
        if challenge_type in {"virtual", "all"} and status in {"active", "all"}:
            calls.append(
                ("active_virtual_challenges", "get_inprogress_virtual_challenges", position + 1)
            )

        data: dict[str, Any] = {}
        category_counts: dict[str, int] = {}
        has_more = False
        upstream_limit = page_size + 1
        for key, method, upstream_start in calls:
            try:
                if method == "get_available_badge_challenges":
                    value = await client.call(
                        "get_available_badge_challenges", upstream_start, upstream_limit
                    )
                elif method == "get_non_completed_badge_challenges":
                    value = await client.call(
                        "get_non_completed_badge_challenges", upstream_start, upstream_limit
                    )
                elif method == "get_badge_challenges":
                    value = await client.call(
                        "get_badge_challenges", upstream_start, upstream_limit
                    )
                elif method == "get_adhoc_challenges":
                    value = await client.call(
                        "get_adhoc_challenges", upstream_start, upstream_limit
                    )
                elif method == "get_inprogress_virtual_challenges":
                    value = await client.call(
                        "get_inprogress_virtual_challenges", upstream_start, upstream_limit
                    )
                else:
                    raise AssertionError("Unknown bounded challenge category")
                normalized, count, category_has_more = _normalize_challenge_page(
                    value,
                    page_size,
                    position,
                )
                data[key] = normalized
                category_counts[key] = count
                has_more = has_more or category_has_more
            except QueryBudgetError:
                raise
            except GarminAPIError:
                raise
            except Exception:
                data[key] = None
                category_counts[key] = 0

        unavailable = []
        if challenge_type in {"adhoc", "all"} and status != "all":
            data["adhoc_challenges"] = None
            unavailable.append("adhoc_challenges")
        if status in {"earned", "all"} and challenge_type in {"badge", "all"}:
            data["earned_badges"] = None
            unavailable.append("earned_badges")

        total_items = sum(category_counts.values())
        budget = current_request_budget()
        if budget is not None:
            budget.reserve_items(total_items)
        if has_more:
            next_position = position + page_size
            validate_continuation_headroom(next_position, (2 * page_size) + 1)
            next_cursor = encode_continuation_cursor(
                surface=surface,
                position=next_position,
                page_size=page_size,
                filters=filters,
            )
        else:
            next_cursor = None
        pagination: dict[str, Any] = {
            "cursor": next_cursor,
            "has_more": has_more,
            "limit": page_size,
            "returned": total_items,
        }
        response = ResponseBuilder.build_response(
            data=data,
            analysis={
                "insights": [f"Retrieved bounded challenge categories: {', '.join(data) or 'none'}"]
            },
            metadata={
                "status": status,
                "challenge_type": challenge_type,
                "category_counts": category_counts,
                "unavailable": unavailable,
            },
            pagination=pagination,
            surface=surface,
        )
        if budget is not None and has_more and not ResponseBuilder.result_contains_error(response):
            budget.note_continuation()
        return response
    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as exc:
        return ResponseBuilder.build_exception_response(exc)
    except Exception as exc:
        return ResponseBuilder.build_exception_response(exc)
