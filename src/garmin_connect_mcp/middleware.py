"""Middleware that injects the shared token-authenticated Garmin client."""

from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .client import GarminAPIError
from .session import GarminSessionManager, get_session_manager
from .write_policy import MUTATION_TOOLS, WritePolicy


class ConfigMiddleware(Middleware):
    """Inject one process-wide Garmin session into tool context."""

    def __init__(
        self,
        session_manager: GarminSessionManager | None = None,
        write_policy: WritePolicy | None = None,
    ):
        self._session_manager = session_manager
        self._write_policy = write_policy or WritePolicy.from_environment()

    async def on_call_tool(self, context: MiddlewareContext, call_next: Callable[..., Any]):
        """Inject a least-privilege client after enforcing the write policy."""
        try:
            tool_name = context.message.name
            arguments = context.message.arguments or {}
        except AttributeError as exc:
            raise ToolError("Unable to resolve the requested tool for authorization.") from exc

        manager = self._session_manager or get_session_manager()
        try:
            operation = self._write_policy.operation_for_call(tool_name, arguments)
            if tool_name in MUTATION_TOOLS:
                if operation is None:
                    # Dry-runs validate and preview locally without loading tokens or
                    # exposing even a read-capable Garmin client.
                    return await call_next(context)
                client = manager.get_mutation_client(operation)
            else:
                client = manager.get_read_client()
        except GarminAPIError as exc:
            raise ToolError(exc.message) from exc

        if context.fastmcp_context:
            await context.fastmcp_context.set_state(
                "client",
                client,
                serializable=False,
            )

        return await call_next(context)
