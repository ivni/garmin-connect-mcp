"""Workout management tools for Garmin Connect MCP server."""

import base64
import hashlib
import json
from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder


async def query_workouts(
    action: Annotated[str, "Action: 'list', 'get', or 'download'"],
    workout_id: Annotated[int | None, "Workout ID (for get/download actions)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Manage structured workouts.

    Actions:
    - list: Get all workouts
    - get: Get specific workout by ID
    - download: Download workout file
    """
    assert ctx is not None
    try:
        client = await ctx.get_state("client")

        if action == "list":
            workouts = client.safe_call("get_workouts")
            return ResponseBuilder.build_response(
                data={
                    "workouts": workouts,
                    "count": len(workouts) if isinstance(workouts, list) else 0,
                },
                metadata={"action": "list"},
            )

        elif action == "get":
            if workout_id is None or workout_id <= 0:
                return ResponseBuilder.build_error_response(
                    "A positive workout ID is required for get action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )

            workout = client.safe_call("get_workout_by_id", workout_id)
            return ResponseBuilder.build_response(
                data={"workout": workout},
                metadata={"action": "get", "workout_id": workout_id},
            )

        elif action == "download":
            if workout_id is None or workout_id <= 0:
                return ResponseBuilder.build_error_response(
                    "A positive workout ID is required for download action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )

            download_info = client.safe_call("download_workout", workout_id)
            if not isinstance(download_info, bytes | bytearray):
                raise GarminAPIError(
                    "garminconnect returned an incompatible workout download response"
                )
            workout_file = bytes(download_info)
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
            )

        else:
            return ResponseBuilder.build_error_response(
                f"Invalid action: {action}",
                "invalid_parameters",
                ["Valid actions: 'list', 'get', 'download'"],
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")


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
            )
        if idempotency_key is None:
            raise ValueError("idempotency_key is required when dry_run is false")
        assert ctx is not None
        client = await ctx.get_state("client")
        result = client.mutate(
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
        )
    except ValueError as e:
        return ResponseBuilder.build_error_response(str(e), "invalid_parameters")
    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")


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
