"""Pagination utilities for Garmin Connect MCP server."""

import base64
import hashlib
import hmac
import json
from typing import Any, NotRequired, Required, TypedDict

from .query_budget import (
    POLICY_VERSION,
    BoundedDateRange,
    InvalidContinuationCursorError,
    QueryBudgetPolicy,
    current_request_budget,
    validate_page_size,
)

MAX_CONTINUATION_CURSOR_LENGTH = 1024
MAX_CONTINUATION_POSITION = 1_000_000


class PaginationCursor(TypedDict, total=False):
    """Cursor data structure."""

    page: Required[int]
    filters: dict[str, Any]


class PaginationInfo(TypedDict):
    """Pagination metadata."""

    cursor: str | None
    has_more: bool
    limit: int
    returned: int
    partial: NotRequired[bool]
    truncation_reason: NotRequired[str]


class ContinuationCursor(TypedDict):
    """Versioned continuation cursor with request binding."""

    version: str
    policy_version: str
    surface: str
    fingerprint: str
    position: int
    page_size: int


def encode_cursor(page: int, filters: dict[str, Any] | None = None) -> str:
    """Encode pagination cursor to opaque string.

    Args:
        page: Page number (1-indexed)
        filters: Optional query filters to preserve in cursor

    Returns:
        Base64-encoded cursor string
    """
    data: PaginationCursor = {"page": page}
    if filters:
        data["filters"] = filters

    json_str = json.dumps(data, sort_keys=True)
    return base64.urlsafe_b64encode(json_str.encode()).decode()


def decode_cursor(cursor: str) -> PaginationCursor:
    """Decode pagination cursor from opaque string.

    Args:
        cursor: Base64-encoded cursor string

    Returns:
        Decoded cursor data

    Raises:
        ValueError: If cursor is invalid
    """
    try:
        json_str = base64.urlsafe_b64decode(cursor.encode()).decode()
        return json.loads(json_str)
    except Exception as e:
        raise ValueError(f"Invalid pagination cursor: {e}") from e


def build_pagination_info(
    *,
    returned_count: int,
    limit: int,
    current_page: int,
    has_more: bool,
    filters: dict[str, Any] | None = None,
) -> PaginationInfo:
    """Build pagination metadata for response.

    Args:
        returned_count: Number of items in current response
        limit: Maximum items per page
        current_page: Current page number (1-indexed)
        has_more: Whether more pages are available
        filters: Query filters to encode in next cursor

    Returns:
        Pagination metadata dict
    """
    next_cursor = None
    if has_more:
        next_cursor = encode_cursor(current_page + 1, filters)

    return {
        "cursor": next_cursor,
        "has_more": has_more,
        "limit": limit,
        "returned": returned_count,
    }


def query_fingerprint(surface: str, filters: dict[str, Any]) -> str:
    """Create a non-reversible stable binding for public query arguments."""
    canonical = json.dumps(
        {"surface": surface, "filters": filters},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def validate_continuation_headroom(position: int, maximum_advance: int) -> None:
    """Refuse a continuation that cannot represent its next bounded advance."""
    if (
        position < 0
        or maximum_advance <= 0
        or position + maximum_advance > MAX_CONTINUATION_POSITION
    ):
        raise InvalidContinuationCursorError


def encode_continuation_cursor(
    *,
    surface: str,
    position: int,
    page_size: int,
    filters: dict[str, Any],
) -> str:
    """Encode a validated continuation position.

    The cursor is intentionally unsigned. It cannot relax any server-side
    budget, and decode_continuation_cursor revalidates every field and binds it
    to the caller-supplied filters.
    """
    if not 0 <= position <= MAX_CONTINUATION_POSITION or page_size <= 0:
        raise ValueError("Invalid continuation cursor position")
    data: ContinuationCursor = {
        "version": "1",
        "policy_version": POLICY_VERSION,
        "surface": surface,
        "fingerprint": query_fingerprint(surface, filters),
        "position": position,
        "page_size": page_size,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode()


def decode_continuation_cursor(
    cursor: str | None,
    *,
    surface: str,
    filters: dict[str, Any],
    policy: QueryBudgetPolicy,
) -> tuple[int, int]:
    """Validate and decode a continuation cursor for one exact query."""
    if cursor is None:
        return 0, policy.default_page_items
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= MAX_CONTINUATION_CURSOR_LENGTH:
        raise ValueError("Invalid continuation cursor")
    try:
        raw = base64.b64decode(cursor.encode(), altchars=b"-_", validate=True)
        data = json.loads(raw.decode())
    except Exception as exc:
        raise ValueError("Invalid continuation cursor") from exc
    if not isinstance(data, dict) or set(data) != {
        "version",
        "policy_version",
        "surface",
        "fingerprint",
        "position",
        "page_size",
    }:
        raise ValueError("Invalid continuation cursor")
    expected_fingerprint = query_fingerprint(surface, filters)
    if (
        data["version"] != "1"
        or data["policy_version"] != POLICY_VERSION
        or data["surface"] != surface
        or not isinstance(data["fingerprint"], str)
        or not hmac.compare_digest(data["fingerprint"], expected_fingerprint)
        or not isinstance(data["position"], int)
        or isinstance(data["position"], bool)
        or not 0 <= data["position"] <= MAX_CONTINUATION_POSITION
        or not isinstance(data["page_size"], int)
        or isinstance(data["page_size"], bool)
        or not 1 <= data["page_size"] <= policy.max_page_items
        # A caller-controlled cursor must leave room both for this lookahead fetch
        # and for the next cursor that a full page may require. Reject it before
        # spending an upstream call rather than failing during response assembly.
        or data["position"] + (2 * data["page_size"]) + 1 > MAX_CONTINUATION_POSITION
    ):
        raise ValueError("Invalid continuation cursor")
    return data["position"], data["page_size"]


def paginate_date_range(
    bounded_range: BoundedDateRange,
    *,
    surface: str,
    filters: dict[str, Any],
    policy: QueryBudgetPolicy,
    cursor: str | None,
    requested_page_size: str | int | None,
) -> tuple[tuple[str, ...], PaginationInfo, int]:
    """Return one validated arithmetic date page."""
    if cursor is None:
        position = 0
        page_size = validate_page_size(requested_page_size, policy)
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
        if requested_page_size is not None:
            requested = validate_page_size(requested_page_size, policy)
            if requested != page_size:
                raise InvalidContinuationCursorError
    if position > bounded_range.day_count:
        raise InvalidContinuationCursorError
    dates, next_position = bounded_range.page(position, page_size)
    next_cursor = None
    if next_position is not None:
        next_cursor = encode_continuation_cursor(
            surface=surface,
            position=next_position,
            page_size=page_size,
            filters=filters,
        )
        budget = current_request_budget()
        if budget is not None:
            budget.note_truncation("page_items")
    return (
        dates,
        {
            "cursor": next_cursor,
            "has_more": next_cursor is not None,
            "limit": page_size,
            "returned": len(dates),
        },
        position,
    )
