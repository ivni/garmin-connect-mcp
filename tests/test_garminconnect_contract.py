"""Offline contract checks against the exact supported garminconnect package."""

from __future__ import annotations

import ast
import importlib.metadata
import inspect
import types
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

import pytest
from garminconnect import Garmin

from garmin_connect_mcp import server
from garmin_connect_mcp.compatibility import (
    COMPATIBILITY_MATRIX,
    SUPPORTED_GARMINCONNECT_VERSION,
    dependency_methods,
)
from garmin_connect_mcp.write_policy import MUTATION_TOOLS, READ_METHODS

PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src" / "garmin_connect_mcp"

CallShape = tuple[str, int, frozenset[str]]

SOURCE_FUNCTION_SURFACES = {
    ("server.py", "athlete_profile_resource"): "garmin://athlete/profile",
    ("server.py", "training_readiness_resource"): "garmin://training/readiness",
    ("server.py", "health_today_resource"): "garmin://health/today",
    ("tools/activities.py", "_query_activities_paginated"): "query_activities",
    ("tools/activities.py", "_query_activities_general_paginated"): "query_activities",
}


def _return_shapes(annotation: Any) -> frozenset[str]:
    origin = get_origin(annotation)
    if origin in {types.UnionType, Union}:
        return frozenset().union(*(_return_shapes(item) for item in get_args(annotation)))
    if annotation is type(None):
        return frozenset({"null"})
    if annotation is bytes:
        return frozenset({"binary"})
    if annotation is str:
        return frozenset({"string"})
    if annotation in {int, float}:
        return frozenset({"number"})
    if annotation is dict or origin is dict:
        return frozenset({"object"})
    if annotation is list or origin is list:
        return frozenset({"array"})
    raise AssertionError(f"Unmodelled dependency return annotation: {annotation!r}")


def test_project_and_runtime_pin_the_supported_dependency_version():
    assert importlib.metadata.version("garminconnect") == SUPPORTED_GARMINCONNECT_VERSION
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'"garminconnect=={SUPPORTED_GARMINCONNECT_VERSION}"' in pyproject


@pytest.mark.asyncio
async def test_every_registered_tool_and_resource_has_a_contract():
    tools = await server.mcp.list_tools()
    resources = await server.mcp.list_resources()

    registered_tools = {tool.name for tool in tools}
    registered_resources = {str(resource.uri) for resource in resources}
    contracted_tools = {
        name for name, contract in COMPATIBILITY_MATRIX.items() if contract.kind == "tool"
    }
    contracted_resources = {
        name for name, contract in COMPATIBILITY_MATRIX.items() if contract.kind == "resource"
    }

    assert contracted_tools == registered_tools
    assert contracted_resources == registered_resources


def test_every_dependency_call_exists_binds_and_has_the_declared_return_shape():
    for surface_name, surface in COMPATIBILITY_MATRIX.items():
        if not surface.supported:
            assert surface.calls == ()
            assert surface.unavailable_reason
            continue
        assert surface.calls, f"Supported surface {surface_name} has no dependency contract"
        for call in surface.calls:
            method = getattr(Garmin, call.method, None)
            assert method is not None, (
                f"{surface_name} references missing garminconnect method {call.method}"
            )
            inspect.signature(method).bind(object(), *call.args, **call.keyword_arguments)
            annotation = get_type_hints(method)["return"]
            assert _return_shapes(annotation) == call.return_shapes, (
                f"{surface_name}:{call.method} return shape changed"
            )


def _surface_for_source_function(relative_path: str, function_name: str) -> str:
    mapped = SOURCE_FUNCTION_SURFACES.get((relative_path, function_name))
    if mapped is not None:
        return mapped
    contract = COMPATIBILITY_MATRIX.get(function_name)
    if contract is not None and contract.kind == "tool":
        return function_name
    raise AssertionError(
        f"Dependency dispatch in {relative_path}:{function_name} is not assigned to an MCP surface"
    )


def _literal_string(node: ast.expr, *, label: str) -> str:
    assert isinstance(node, ast.Constant) and isinstance(node.value, str), label
    return node.value


def _source_call_shapes() -> dict[str, set[CallShape]]:
    shapes: dict[str, set[CallShape]] = {}

    class CallVisitor(ast.NodeVisitor):
        def __init__(self, relative_path: str):
            self.relative_path = relative_path
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        def visit_Call(self, node: ast.Call) -> None:
            shape = self._dependency_shape(node)
            if shape is not None:
                assert self.functions, f"Module-level dependency dispatch in {self.relative_path}"
                surface = _surface_for_source_function(self.relative_path, self.functions[-1])
                shapes.setdefault(surface, set()).add(shape)
            self.generic_visit(node)

        def _dependency_shape(self, node: ast.Call) -> CallShape | None:
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"safe_call", "mutate"}
                and node.args
            ):
                if not (
                    isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                ):
                    allowed_dynamic_dispatch = (
                        self.relative_path == "client.py"
                        and self.functions[-1] in {"safe_call", "mutate"}
                    ) or (
                        self.relative_path == "tools/data_management.py"
                        and self.functions[-1] == "_execute_health_write"
                    )
                    assert allowed_dynamic_dispatch, (
                        "MCP dependency methods must remain literal so source-contract drift "
                        "cannot be hidden"
                    )
                    return None
                assert not any(isinstance(arg, ast.Starred) for arg in node.args[1:])
                keyword_names = set()
                for keyword in node.keywords:
                    assert keyword.arg is not None, "Dependency calls cannot unpack dynamic kwargs"
                    if node.func.attr != "mutate" or keyword.arg != "idempotency_key":
                        keyword_names.add(keyword.arg)
                return (
                    node.args[0].value,
                    len(node.args) - 1,
                    frozenset(keyword_names),
                )

            if isinstance(node.func, ast.Name) and node.func.id == "_execute_health_write":
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                method = _literal_string(
                    keywords["method_name"], label="Health write method must be literal"
                )
                method_args = keywords["method_args"]
                method_kwargs = keywords["method_kwargs"]
                assert isinstance(method_args, ast.Tuple), "Health write args must be a tuple"
                assert not any(isinstance(arg, ast.Starred) for arg in method_args.elts)
                assert isinstance(method_kwargs, ast.Dict), "Health write kwargs must be a dict"
                keyword_names = frozenset(
                    _literal_string(key, label="Health write kwarg names must be literal")
                    for key in method_kwargs.keys
                )
                return method, len(method_args.elts), keyword_names
            return None

    for path in SOURCE_ROOT.rglob("*.py"):
        relative_path = path.relative_to(SOURCE_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        CallVisitor(relative_path).visit(tree)
    return shapes


def test_actual_source_dispatch_shapes_match_and_bind_to_each_surface_contract():
    mutation_methods = frozenset(operation.method_name for operation in MUTATION_TOOLS.values())
    assert dependency_methods() - mutation_methods == READ_METHODS

    actual = _source_call_shapes()
    for surface_name, surface in COMPATIBILITY_MATRIX.items():
        expected = {
            (call.method, len(call.args), frozenset(dict(call.kwargs))) for call in surface.calls
        }
        assert actual.get(surface_name, set()) == expected, (
            f"Source dispatch drifted from the {surface_name} contract"
        )
        for method_name, positional_count, keyword_names in actual.get(surface_name, set()):
            method = getattr(Garmin, method_name)
            inspect.signature(method).bind(
                object(),
                *(object() for _ in range(positional_count)),
                **{name: object() for name in keyword_names},
            )
