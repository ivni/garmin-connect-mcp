"""Tests for capability policy, restricted facades, and mutation retry safety."""

from __future__ import annotations

import ast
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from garminconnect import GarminConnectConnectionError

from garmin_connect_mcp import server
from garmin_connect_mcp.client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminClientWrapper,
    GarminMethodNotAllowedError,
    GarminMutationClient,
    GarminMutationInProgressError,
    GarminMutationOutcomeUnknownError,
    GarminReadClient,
    MutationOperation,
    MutationRegistry,
)
from garmin_connect_mcp.token_store import TokenStore, TokenStoreLockTimeout
from garmin_connect_mcp.write_policy import READ_METHODS, WritePolicy


class FakeWrapper:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    def safe_call(self, method_name, *args, **kwargs):
        self.calls.append((method_name, args, kwargs))
        if self.error is not None:
            raise self.error
        return {"call": len(self.calls)}


def mutation_client(
    wrapper: FakeWrapper,
    registry: MutationRegistry | None = None,
) -> GarminMutationClient:
    return GarminMutationClient(
        wrapper,  # type: ignore[arg-type]
        MutationOperation(
            capability="weight.write",
            method_name="add_weigh_in",
            reconciliation="query the target date and verify the entry",
        ),
        registry or MutationRegistry(),
    )


def test_write_policy_defaults_to_no_capabilities():
    policy = WritePolicy.from_environment({})

    assert policy.enabled_capabilities == frozenset()


def test_write_policy_rejects_unknown_capability():
    with pytest.raises(ValueError, match="Unknown Garmin write capabilities"):
        WritePolicy.from_environment({"GARMIN_WRITE_CAPABILITIES": "weight.typo"})


def test_read_facade_rejects_mutation_before_underlying_client_call():
    wrapper = FakeWrapper()
    client = GarminReadClient(wrapper, frozenset({"get_stats"}))  # type: ignore[arg-type]

    assert client.safe_call("get_stats", "today") == {"call": 1}
    with pytest.raises(GarminMethodNotAllowedError, match="read-only client"):
        client.safe_call("add_weigh_in", 75)

    assert wrapper.calls == [("get_stats", ("today",), {})]


def test_mutation_facade_rejects_unrelated_method_before_underlying_call():
    wrapper = FakeWrapper()
    client = mutation_client(wrapper)

    with pytest.raises(GarminMethodNotAllowedError, match="not authorized"):
        client.mutate(
            "add_hydration_data",
            500,
            idempotency_key="request-0001",
        )

    assert wrapper.calls == []


def test_successful_retry_with_same_key_is_deduplicated():
    wrapper = FakeWrapper()
    client = mutation_client(wrapper)

    first = client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    second = client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert first == second == {"call": 1}
    assert len(wrapper.calls) == 1


def test_full_registry_refuses_new_key_without_evicting_success():
    wrapper = FakeWrapper()
    client = mutation_client(wrapper, MutationRegistry(max_records=1))

    first = client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    with pytest.raises(GarminAPIError, match="ledger is full"):
        client.mutate("add_weigh_in", 76, idempotency_key="request-0002")
    replay = client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert first == replay == {"call": 1}
    assert len(wrapper.calls) == 1


def test_confirmed_key_is_deduplicated_after_registry_restart(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    first_wrapper = FakeWrapper()
    first_client = mutation_client(
        first_wrapper,
        MutationRegistry(journal=store),
    )
    first_client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    restarted_wrapper = FakeWrapper()
    restarted_client = mutation_client(
        restarted_wrapper,
        MutationRegistry(journal=TokenStore(store.directory)),
    )
    result = restarted_client.mutate(
        "add_weigh_in",
        75,
        idempotency_key="request-0001",
    )

    assert result["deduplicated"] is True
    assert restarted_wrapper.calls == []
    assert store.mutation_ledger_file.is_file()


def test_key_reuse_with_different_inputs_is_rejected():
    wrapper = FakeWrapper()
    client = mutation_client(wrapper)
    client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    with pytest.raises(GarminAPIError, match="different mutation inputs"):
        client.mutate("add_weigh_in", 76, idempotency_key="request-0001")

    assert len(wrapper.calls) == 1


def test_ambiguous_outcome_is_quarantined_with_reconciliation_guidance():
    wrapper = FakeWrapper(GarminAPIError("connection lost", OSError("reset")))
    client = mutation_client(wrapper)

    with pytest.raises(
        GarminMutationOutcomeUnknownError,
        match="must not be retried automatically.*query the target date",
    ):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert len(wrapper.calls) == 1


def test_response_json_failure_after_dispatch_is_quarantined():
    wrapper = FakeWrapper(json.JSONDecodeError("invalid response", "<html>", 0))
    client = mutation_client(wrapper)

    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert len(wrapper.calls) == 1


def test_invocation_attribute_error_after_dispatch_is_quarantined():
    wrapper = FakeWrapper(AttributeError("response object has no attribute 'payload'"))
    client = mutation_client(wrapper)

    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert len(wrapper.calls) == 1


def test_server_error_detail_cannot_disguise_ambiguous_500_as_definite_404():
    class MisleadingServerErrorClient:
        def __init__(self):
            self.calls = 0

        def add_weigh_in(self, *_args, **_kwargs):
            self.calls += 1
            raise GarminConnectConnectionError("API Error 500: upstream returned 404 Unauthorized")

    garmin = MisleadingServerErrorClient()
    wrapper = GarminClientWrapper(garmin)  # type: ignore[arg-type]
    client = mutation_client(wrapper)  # type: ignore[arg-type]

    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    with pytest.raises(GarminMutationOutcomeUnknownError, match="must not be retried"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert garmin.calls == 1


def test_definite_failure_releases_key_for_corrected_retry():
    wrapper = FakeWrapper(GarminAuthenticationError("authentication rejected"))
    client = mutation_client(wrapper)

    with pytest.raises(GarminAuthenticationError, match="authentication rejected"):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")
    wrapper.error = None

    assert client.mutate("add_weigh_in", 75, idempotency_key="request-0001") == {"call": 2}
    assert len(wrapper.calls) == 2


def test_concurrent_same_key_waits_for_live_call_and_deduplicates():
    started = threading.Event()
    release = threading.Event()

    class BlockingWrapper(FakeWrapper):
        def safe_call(self, method_name, *args, **kwargs):
            self.calls.append((method_name, args, kwargs))
            started.set()
            assert release.wait(timeout=5)
            return {"call": len(self.calls)}

    wrapper = BlockingWrapper()
    client = mutation_client(wrapper)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.mutate,
            "add_weigh_in",
            75,
            idempotency_key="request-0001",
        )
        assert started.wait(timeout=5)
        retry_entered = threading.Event()

        def retry():
            retry_entered.set()
            return client.mutate(
                "add_weigh_in",
                75,
                idempotency_key="request-0001",
            )

        second = executor.submit(retry)
        assert retry_entered.wait(timeout=5)
        assert not second.done()
        release.set()

        assert first.result(timeout=5) == {"call": 1}
        assert second.result(timeout=5) == {"call": 1}

    assert len(wrapper.calls) == 1


def test_busy_interprocess_transaction_requires_same_key_retry():
    class BusyTransaction:
        def __enter__(self):
            raise TokenStoreLockTimeout("Timed out waiting for the mutation journal lock")

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class BusyJournal:
        def mutation_transaction(self):
            return BusyTransaction()

    wrapper = FakeWrapper()
    client = mutation_client(wrapper, MutationRegistry(journal=BusyJournal()))

    with pytest.raises(
        GarminMutationInProgressError,
        match="Wait, then retry.*same idempotency_key.*do not use a new key",
    ):
        client.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert wrapper.calls == []


def test_abandoned_in_progress_record_is_quarantined_after_lock_release(tmp_path: Path):
    class InterruptedWrapper(FakeWrapper):
        def safe_call(self, method_name, *args, **kwargs):
            self.calls.append((method_name, args, kwargs))
            raise KeyboardInterrupt

    store = TokenStore(tmp_path / "tokens")
    interrupted = mutation_client(
        InterruptedWrapper(),
        MutationRegistry(journal=store),
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    restarted_wrapper = FakeWrapper()
    restarted = mutation_client(
        restarted_wrapper,
        MutationRegistry(journal=TokenStore(store.directory)),
    )
    with pytest.raises(
        GarminMutationOutcomeUnknownError,
        match="must not be retried automatically.*query the target date",
    ):
        restarted.mutate("add_weigh_in", 75, idempotency_key="request-0001")

    assert restarted_wrapper.calls == []


def test_invalid_idempotency_key_fails_before_underlying_call():
    wrapper = FakeWrapper()
    client = mutation_client(wrapper)

    with pytest.raises(GarminAPIError, match="idempotency_key must be 8-128"):
        client.mutate("add_weigh_in", 75, idempotency_key="short")

    assert wrapper.calls == []


@pytest.mark.asyncio
async def test_registered_write_annotations_match_operation_risk():
    delete_tool = await server.mcp.get_tool("delete_weight_entries")
    add_tool = await server.mcp.get_tool("add_weight_entry")
    query_tool = await server.mcp.get_tool("query_workouts")

    assert delete_tool is not None
    assert delete_tool.annotations is not None
    assert delete_tool.annotations.readOnlyHint is False
    assert delete_tool.annotations.destructiveHint is True
    assert add_tool is not None
    assert add_tool.annotations is not None
    assert add_tool.annotations.readOnlyHint is False
    assert add_tool.annotations.destructiveHint is False
    assert query_tool is not None
    assert query_tool.annotations is not None
    assert query_tool.annotations.readOnlyHint is True


def test_every_literal_read_safe_call_is_allowlisted():
    source_root = Path(__file__).parents[1] / "src" / "garmin_connect_mcp"
    called_methods: set[str] = set()
    for source_path in source_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "safe_call"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                called_methods.add(node.args[0].value)

    assert called_methods <= READ_METHODS
