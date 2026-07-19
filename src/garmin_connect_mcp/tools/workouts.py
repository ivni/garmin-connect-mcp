"""Workout management tools for Garmin Connect MCP server."""

import base64
import hashlib
import json
from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..pagination import (
    decode_continuation_cursor,
    encode_continuation_cursor,
    validate_continuation_headroom,
)
from ..query_budget import (
    InvalidContinuationCursorError,
    QueryBudgetError,
    ResponseBudgetExceededError,
    current_request_budget,
    policy_for_surface,
    validate_page_size,
)
from ..response_builder import ResponseBuilder


async def query_workouts(
    action: Annotated[str, "Action: 'list', 'get', or 'download'"],
    workout_id: Annotated[int | None, "Workout ID (for get/download actions)"] = None,
    cursor: Annotated[str | None, "Continuation cursor for list action"] = None,
    limit: Annotated[int | None, "List page size (1-50)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Manage structured workouts.

    Actions:
    - list: Get all workouts
    - get: Get specific workout by ID
    - download: Download workout file
    """
    if action not in {"list", "get", "download"}:
        return ResponseBuilder.build_error_response(
            f"Invalid action: {action}",
            "invalid_parameters",
            ["Valid actions: 'list', 'get', 'download'"],
        )
    if action in {"get", "download"} and (workout_id is None or workout_id <= 0):
        return ResponseBuilder.build_error_response(
            f"A positive workout ID is required for {action} action",
            "invalid_parameters",
            ["Provide workout_id parameter"],
        )
    if action != "list" and (cursor is not None or limit is not None):
        return ResponseBuilder.build_error_response(
            "cursor and limit are valid only for list action",
            "invalid_parameters",
        )

    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        if action == "list":
            surface = "query_workouts"
            policy = policy_for_surface(surface)
            filters = {"action": "list"}
            if cursor is None:
                position = 0
                page_size = validate_page_size(limit, policy)
            else:
                try:
                    position, page_size = decode_continuation_cursor(
                        cursor,
                        surface=surface,
                        filters=filters,
                        policy=policy,
                    )
                except ValueError as exc:
                    raise InvalidContinuationCursorError from exc
                if limit is not None and validate_page_size(limit, policy) != page_size:
                    raise InvalidContinuationCursorError
            raw_workouts = await client.call("get_workouts", position, page_size + 1)
            if not isinstance(raw_workouts, list):
                raise GarminAPIError("garminconnect returned an incompatible workout list")
            has_more = len(raw_workouts) > page_size
            workouts = raw_workouts[:page_size]

            def workout_cursor(count: int) -> str:
                next_position = position + count
                validate_continuation_headroom(next_position, (2 * page_size) + 1)
                return encode_continuation_cursor(
                    surface=surface,
                    position=next_position,
                    page_size=page_size,
                    filters=filters,
                )

            if has_more:
                next_cursor = workout_cursor(len(workouts))
            else:
                next_cursor = None
            pagination = {
                "cursor": next_cursor,
                "has_more": has_more,
                "limit": page_size,
                "returned": len(workouts),
                **({"partial": True, "truncation_reason": "page_limit"} if has_more else {}),
            }
            budget = current_request_budget()
            if budget is not None and has_more:
                budget.note_truncation("page_items")
            return ResponseBuilder.build_bounded_collection_response(
                items=workouts,
                data_factory=lambda values: {
                    "workouts": values,
                    "count": len(values),
                },
                metadata_factory=lambda _count: {"action": "list"},
                pagination=pagination,
                cursor_factory=workout_cursor,
                surface="query_workouts",
            )

        if action == "get":
            assert workout_id is not None
            workout = await client.call("get_workout_by_id", workout_id)
            return ResponseBuilder.build_response(
                data={"workout": workout},
                metadata={"action": "get", "workout_id": workout_id},
                surface="query_workouts",
            )

        if action == "download":
            assert workout_id is not None
            download_info = await client.call("download_workout", workout_id)
            if not isinstance(download_info, bytes | bytearray):
                raise GarminAPIError(
                    "garminconnect returned an incompatible workout download response"
                )
            workout_file = bytes(download_info)
            budget = current_request_budget()
            if budget is not None:
                # Base64 expands by 4/3 and FastMCP places the string in both text
                # content and structuredContent. Leave a fixed envelope for JSON,
                # metadata, and the remaining ToolResult fields.
                max_raw_bytes = max(0, (budget.policy.max_response_bytes - 16 * 1024) * 3 // 8)
                if len(workout_file) > max_raw_bytes:
                    raise ResponseBudgetExceededError
            return ResponseBuilder.build_response(
                data={
                    "workout_file": {
                        "content_base64": base64.b64encode(workout_file).decode("ascii"),
                        "content_type": "application/vnd.garmin.fit",
                        "encoding": "base64",
                        "sha256": hashlib.sha256(workout_file).hexdigest(),
                        "size_bytes": len(workout_file),
                    }
                },
                metadata={"action": "download", "workout_id": workout_id},
                surface="query_workouts",
            )

        raise AssertionError("Validated workout action was not dispatched")

    except QueryBudgetError as exc:
        return ResponseBuilder.build_budget_error_response(exc)
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


async def upload_workout(
    workout_data: Annotated[
        str,
        "Workout JSON object or array, maximum 256 KiB and nesting depth 20",
    ],
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
    """Validate, preview, or upload a structured workout."""
    try:
        size, digest = _validate_workout_data(workout_data)
        preview = {"size_bytes": size, "sha256": digest}
        if dry_run:
            return ResponseBuilder.build_response(
                data={"preview": preview},
                analysis={"insights": ["Dry-run only; Garmin was not contacted"]},
                metadata={"dry_run": True, "capability": "workouts.upload"},
                surface="upload_workout",
            )
        if idempotency_key is None:
            raise ValueError("idempotency_key is required when dry_run is false")
        assert ctx is not None
        client = await ctx.get_state("client")
        result = await client.mutate(
            "upload_workout",
            workout_data,
            idempotency_key=idempotency_key,
        )
        return ResponseBuilder.build_response(
            data={"result": result, **preview},
            analysis={"insights": ["Workout uploaded successfully"]},
            metadata={
                "dry_run": False,
                "capability": "workouts.upload",
                "idempotency_key": idempotency_key,
            },
            surface="upload_workout",
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), "invalid_parameters")
    except GarminAPIError as e:
        return ResponseBuilder.build_exception_response(e)
    except Exception as e:
        return ResponseBuilder.build_exception_response(e)


def _validate_workout_data(value: str) -> tuple[int, str]:
    encoded = value.encode("utf-8")
    if not encoded or len(encoded) > 256 * 1024:
        raise ValueError("workout_data must contain from 1 byte through 256 KiB of JSON")
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"workout_data must be valid JSON: {exc.msg}") from exc
    if not isinstance(payload, dict | list):
        raise ValueError("workout_data must be a JSON object or array")

    nodes = 0
    stack = [(payload, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > 10_000:
            raise ValueError("workout_data must contain no more than 10,000 JSON values")
        if depth > 20:
            raise ValueError("workout_data nesting depth must not exceed 20")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)

    return len(encoded), hashlib.sha256(encoded).hexdigest()
