"""Small fail-closed helpers for public response projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

Projector = Callable[[Any], Any]


def project_tree(value: Any, allowed_fields: frozenset[str]) -> Any:
    """Recursively retain only explicitly allowed mapping keys."""
    if isinstance(value, Mapping):
        return {
            str(key): project_tree(item, allowed_fields)
            for key, item in value.items()
            if isinstance(key, str) and key in allowed_fields
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [project_tree(item, allowed_fields) for item in value]
    return value


def project_object(
    value: Any,
    *,
    scalar_fields: frozenset[str] = frozenset(),
    field_projectors: Mapping[str, Projector] | None = None,
) -> dict[str, Any]:
    """Project one mapping with field-specific nested projectors."""
    if not isinstance(value, Mapping):
        return {}
    projectors = field_projectors or {}
    projected: dict[str, Any] = {}
    for key in scalar_fields:
        if key in value:
            projected[key] = value[key]
    for key, projector in projectors.items():
        if key in value:
            projected[key] = projector(value[key])
    return projected


def project_list(value: Any, projector: Projector) -> list[Any]:
    """Project a sequence while rejecting scalar and mapping lookalikes."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    return [projector(item) for item in value]


def project_dict_or_list(value: Any, projector: Projector) -> Any:
    """Project an upstream object-or-array response."""
    if isinstance(value, Mapping):
        return projector(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [projector(item) for item in value]
    return None


def project_date(value: Any) -> Any:
    """Preserve the server-generated date structure only."""
    return project_tree(
        value,
        frozenset({"datetime", "date", "day_of_week", "formatted"}),
    )


def project_write_result(value: Any) -> Any:
    """Retain only stable acknowledgement fields from mutation responses."""
    acknowledgement: dict[str, Any] = {"acknowledged": True}
    if isinstance(value, bool):
        acknowledgement["success"] = value
    elif isinstance(value, int):
        acknowledgement["count"] = value
    elif isinstance(value, Mapping):
        if isinstance(value.get("success"), bool):
            acknowledgement["success"] = value["success"]
        if isinstance(value.get("deduplicated"), bool):
            acknowledgement["deduplicated"] = value["deduplicated"]
        for identifier in ("activityId", "workoutId"):
            candidate = value.get(identifier)
            if isinstance(candidate, int | str) and not isinstance(candidate, bool):
                acknowledgement[identifier] = candidate
        if isinstance(value.get("count"), int) and not isinstance(value["count"], bool):
            acknowledgement["count"] = value["count"]
    return acknowledgement


def project_write_response(
    value: Any, _context: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Project the shared preview/result envelope used by write tools."""
    return project_object(
        value,
        scalar_fields=frozenset(
            {
                "date",
                "weight",
                "size_bytes",
                "sha256",
                "delete_all",
                "required_confirmation",
            }
        ),
        field_projectors={
            "preview": lambda item: project_tree(
                item,
                frozenset(
                    {
                        "date",
                        "data",
                        "weight",
                        "body_fat",
                        "body_water",
                        "systolic",
                        "diastolic",
                        "pulse",
                        "volume_ml",
                        "size_bytes",
                        "sha256",
                        "delete_all",
                        "required_confirmation",
                    }
                ),
            ),
            "data": lambda item: project_tree(
                item,
                frozenset(
                    {
                        "weight",
                        "body_fat",
                        "body_water",
                        "systolic",
                        "diastolic",
                        "pulse",
                        "volume_ml",
                    }
                ),
            ),
            "result": project_write_result,
        },
    )
