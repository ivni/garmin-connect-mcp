"""Middleware that injects the shared token-authenticated Garmin client."""

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .client import GarminAPIError
from .session import GarminSessionManager, get_session_manager


class ConfigMiddleware(Middleware):
    """Inject one process-wide Garmin session into tool context."""

    def __init__(self, session_manager: GarminSessionManager | None = None):
        self._session_manager = session_manager

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        """Resolve the token-only client before each tool call."""
        manager = self._session_manager or get_session_manager()
        try:
            client = manager.get_client()
        except GarminAPIError as exc:
            raise ToolError(exc.message) from exc

        if context.fastmcp_context:
            await context.fastmcp_context.set_state(
                "client",
                client,
                serializable=False,
            )

        return await call_next(context)
