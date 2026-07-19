"""Executable coverage contract for every public MCP response surface."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from garmin_connect_mcp import server
from garmin_connect_mcp.compatibility import COMPATIBILITY_MATRIX
from garmin_connect_mcp.response_policy import EXPOSURE_POLICIES, PROJECTORS


@pytest.mark.asyncio
async def test_every_registered_surface_has_one_exposure_policy_and_projector():
    tools = await server.mcp.list_tools()
    resources = await server.mcp.list_resources()
    registered = {tool.name for tool in tools} | {str(resource.uri) for resource in resources}

    assert registered == set(COMPATIBILITY_MATRIX)
    assert registered == set(EXPOSURE_POLICIES)
    assert registered == set(PROJECTORS)


def test_no_surface_uses_undocumented_raw_passthrough():
    assert all(policy.raw_passthrough_reason is None for policy in EXPOSURE_POLICIES.values())


def test_every_success_response_in_public_modules_names_its_surface():
    source_root = Path("src/garmin_connect_mcp")
    paths = [source_root / "server.py", *(source_root / "tools").glob("*.py")]
    missing: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "build_response":
                continue
            if not any(keyword.arg == "surface" for keyword in node.keywords):
                missing.append(f"{path}:{node.lineno}")

    assert missing == []


def test_location_opt_in_is_declared_only_on_activity_surfaces():
    opted_in = {
        surface
        for surface, policy in EXPOSURE_POLICIES.items()
        if "include_location" in policy.opt_ins
    }
    assert opted_in == {"query_activities", "get_activity_details"}
