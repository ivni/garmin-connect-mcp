"""Middleware that injects the shared token-authenticated Garmin client."""

import asyncio
from collections.abc import Callable
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.resources import ResourceResult
from fastmcp.server.middleware import Middleware, MiddlewareContext

from .query_budget import (
    BudgetedGarminMutationClient,
    BudgetedGarminReadClient,
    QueryBudgetError,
    RequestBudget,
    log_budget_outcome,
    policy_for_surface,
    reset_current_request_budget,
    run_bounded_finalization,
    set_current_request_budget,
)
from .response_builder import ResponseBuilder
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
        budget: RequestBudget | None = None
        budget_token = None
        try:
            tool_name = context.message.name
            arguments = context.message.arguments or {}
            budget = RequestBudget(tool_name, policy_for_surface(tool_name))
            budget_token = set_current_request_budget(budget)
            try:
                async with asyncio.timeout(budget.remaining_seconds()):
                    manager = self._session_manager or get_session_manager()
                    operation = self._write_policy.operation_for_call(tool_name, arguments)
                    dry_run = tool_name in MUTATION_TOOLS and operation is None
                    if tool_name in MUTATION_TOOLS and not dry_run:
                        client = BudgetedGarminMutationClient.lazy(
                            lambda: manager.get_mutation_client(
                                operation,
                                budget.ensure_active,
                            ),
                            budget,
                        )
                    elif not dry_run:
                        client = BudgetedGarminReadClient.lazy(
                            lambda: manager.get_read_client(budget.ensure_active),
                            budget,
                        )

                    if context.fastmcp_context and not dry_run:
                        await context.fastmcp_context.set_state(
                            "client",
                            client,
                            serializable=False,
                        )
                        await context.fastmcp_context.set_state(
                            "request_budget",
                            budget,
                            serializable=False,
                        )

                    result = await call_next(context)
                    if budget.mutation_confirmed and ResponseBuilder.result_contains_error(result):
                        budget.note_outcome("confirmed")
                        result = self._confirmed_result(
                            tool_name,
                            omitted_reason="post_confirmation_failure",
                        )
                    if tool_name not in MUTATION_TOOLS or dry_run:
                        budget.ensure_active()
            except TimeoutError as exc:
                budget.expire()
                if budget.mutation_confirmed:
                    budget.note_outcome("confirmed")
                    result = self._confirmed_result(
                        tool_name,
                        omitted_reason="deadline_delivery",
                    )
                    budget.record_response_size(
                        ResponseBuilder.serialized_result_size(result, tool_name)
                    )
                    log_budget_outcome(budget, "confirmed")
                    return result
                elif budget.mutation_uncertain:
                    from .client import GarminMutationOutcomeUnknownError

                    raise GarminMutationOutcomeUnknownError(
                        "The Garmin mutation was submitted but not confirmed before the "
                        "request ended. Reconcile it before retrying with the same "
                        "idempotency_key."
                    ) from exc
                else:
                    from .query_budget import RequestDeadlineExceededError

                    raise RequestDeadlineExceededError from exc

            serialized_size = await run_bounded_finalization(
                lambda: ResponseBuilder.serialized_result_size(result, tool_name),
                budget,
            )
            if serialized_size > budget.policy.max_response_bytes and budget.mutation_confirmed:
                budget.note_outcome("confirmed")
                result = self._confirmed_result(tool_name)
                serialized_size = await run_bounded_finalization(
                    lambda: ResponseBuilder.serialized_result_size(result, tool_name),
                    budget,
                )
            budget.record_response_size(serialized_size)
            log_budget_outcome(budget, budget.outcome_hint or "ok")
            return result
        except asyncio.CancelledError:
            if budget is not None:
                budget.cancel()
                log_budget_outcome(budget, budget.outcome_hint or "cancelled")
            raise
        except ToolError as exc:
            if budget is not None:
                if budget.mutation_confirmed:
                    result = self._confirmed_result(
                        budget.surface,
                        omitted_reason="post_confirmation_failure",
                    )
                    log_budget_outcome(budget, "confirmed")
                    return result
                if budget.mutation_uncertain:
                    response = ResponseBuilder.build_exception_response(
                        self._mutation_unknown_error()
                    )
                    response = self._bounded_tool_error_response(budget, response)
                    log_budget_outcome(budget, "GARMIN_UPSTREAM_UNAVAILABLE")
                    raise ToolError(response) from exc
                response = self._bounded_tool_error_response(budget, str(exc))
                log_budget_outcome(budget, budget.outcome_hint or "tool_error")
                raise ToolError(response) from exc
            raise
        except QueryBudgetError as exc:
            if budget is not None:
                if budget.mutation_confirmed:
                    result = self._confirmed_result(
                        budget.surface,
                        omitted_reason="post_confirmation_failure",
                    )
                    log_budget_outcome(budget, "confirmed")
                    return result
                if budget.mutation_uncertain:
                    response = ResponseBuilder.build_exception_response(
                        self._mutation_unknown_error()
                    )
                    response = self._bounded_tool_error_response(budget, response)
                    log_budget_outcome(budget, "GARMIN_UPSTREAM_UNAVAILABLE")
                    raise ToolError(response) from exc
                response = ResponseBuilder.build_budget_error_response(exc)
                response = self._bounded_tool_error_response(budget, response)
                log_budget_outcome(budget, exc.public_code)
            else:
                response = ResponseBuilder.build_budget_error_response(exc)
            raise ToolError(response) from exc
        except Exception as exc:
            if budget is not None:
                if budget.mutation_confirmed:
                    result = self._confirmed_result(
                        budget.surface,
                        omitted_reason="post_confirmation_failure",
                    )
                    log_budget_outcome(budget, "confirmed")
                    return result
                if budget.mutation_uncertain:
                    response = ResponseBuilder.build_exception_response(
                        self._mutation_unknown_error()
                    )
                    response = self._bounded_tool_error_response(budget, response)
                    log_budget_outcome(budget, "GARMIN_UPSTREAM_UNAVAILABLE")
                    raise ToolError(response) from exc
                response = ResponseBuilder.build_exception_response(exc)
                response = self._bounded_tool_error_response(budget, response)
                log_budget_outcome(budget, budget.outcome_hint or "INTERNAL_ERROR")
            else:
                response = ResponseBuilder.build_exception_response(exc)
            raise ToolError(response) from exc
        finally:
            if budget_token is not None:
                reset_current_request_budget(budget_token)

    @staticmethod
    def _confirmed_result(
        tool_name: str,
        *,
        omitted_reason: str = "response_budget",
    ) -> Any:
        compact = ResponseBuilder.build_confirmed_mutation_budget_response(
            tool_name,
            omitted_reason=omitted_reason,
        )
        from fastmcp.tools.base import ToolResult

        return ToolResult(
            content=compact,
            structured_content={"result": compact},
            meta={"fastmcp": {"wrap_result": True}},
        )

    @staticmethod
    def _bounded_tool_error_response(budget: RequestBudget, response: str) -> str:
        size = ResponseBuilder.serialized_tool_error_size(response)
        if size > budget.policy.max_response_bytes:
            from .query_budget import ResponseBudgetExceededError

            budget.mark_terminal("response_bytes")
            budget.note_truncation("response_bytes")
            response = ResponseBuilder.build_budget_error_response(ResponseBudgetExceededError())
            size = ResponseBuilder.serialized_tool_error_size(response)
        budget.record_response_size(size)
        return response

    @staticmethod
    def _mutation_unknown_error() -> Exception:
        from .client import GarminMutationOutcomeUnknownError

        return GarminMutationOutcomeUnknownError(
            "The Garmin mutation was submitted but not confirmed before response delivery "
            "failed. Reconcile it before retrying with the same idempotency_key."
        )

    async def on_read_resource(
        self,
        context: MiddlewareContext,
        call_next: Callable[..., Any],
    ) -> ResourceResult:
        """Enforce the final serialized envelope even if a resource bypasses builders."""
        surface = str(context.message.uri)
        try:
            policy = policy_for_surface(surface)
        except RuntimeError:
            return await call_next(context)
        guard_budget = RequestBudget(surface, policy)
        try:
            async with asyncio.timeout(guard_budget.remaining_seconds()):
                result = await call_next(context)
            serialized_size = await run_bounded_finalization(
                lambda: ResponseBuilder.serialized_result_size(result, surface),
                guard_budget,
            )
        except TimeoutError:
            guard_budget.expire()
            serialized_size = policy.max_response_bytes + 1
            result = None
        except QueryBudgetError:
            serialized_size = policy.max_response_bytes + 1
            result = None
        if result is not None and serialized_size <= policy.max_response_bytes:
            return result
        from .query_budget import RequestDeadlineExceededError, ResponseBudgetExceededError

        error = (
            RequestDeadlineExceededError()
            if guard_budget.terminal_reason == "deadline"
            else ResponseBudgetExceededError()
        )
        if error.public_code == "RESPONSE_BUDGET_EXCEEDED":
            guard_budget.mark_terminal("response_bytes")
        response = ResponseBuilder.build_budget_error_response(error)
        bounded = ResourceResult(response)
        if error.public_code == "RESPONSE_BUDGET_EXCEEDED":
            guard_budget.note_truncation("response_bytes")
        guard_budget.note_outcome(error.public_code)
        guard_budget.record_response_size(ResponseBuilder.serialized_result_size(bounded, surface))
        log_budget_outcome(guard_budget, error.public_code)
        return bounded
