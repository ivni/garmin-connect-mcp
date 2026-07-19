"""Tests for in-memory sessions and canonical token generations."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from garmin_connect_mcp.auth import GarminConfig
from garmin_connect_mcp.client import GarminAPIError, GarminAuthenticationError
from garmin_connect_mcp.session import (
    GarminSessionManager,
    _inline_token_payload,
    _invalid_inline_token_payload,
)
from garmin_connect_mcp.token_store import TokenStore, TokenStoreError


def token_payload(label: str) -> str:
    return json.dumps(
        {
            "di_token": f"access-{label}",
            "di_refresh_token": f"refresh-{label}",
            "di_client_id": f"client-{label}",
        }
    )


class FakeTokenClient:
    def __init__(self):
        self.label = "missing"

    def loads(self, payload: str) -> None:
        self.label = json.loads(payload)["di_refresh_token"].removeprefix("refresh-")

    def dumps(self) -> str:
        return token_payload(self.label)


class FakeGarmin:
    """Small public-API fake that keeps tokens only in memory."""

    def __init__(self, factory: FakeGarminFactory, kwargs: dict[str, Any]):
        self.factory = factory
        self.kwargs = kwargs
        self.login_inputs: list[str] = []
        self.client = FakeTokenClient()

    def login(self, tokenstore: str) -> None:
        self.login_inputs.append(tokenstore)
        if self.factory.login_error:
            raise self.factory.login_error
        if self.kwargs.get("email"):
            # Bootstrap receives deliberately invalid inline JSON, never a path.
            assert len(tokenstore) > 512
            self.client.label = self.factory.bootstrap_label
        else:
            self.client.loads(tokenstore)
            callback = self.factory.after_runtime_login
            self.factory.after_runtime_login = None
            if callback is not None:
                callback()

    def concurrency_probe(self) -> str:
        with self.factory.probe_lock:
            self.factory.active_calls += 1
            self.factory.max_active_calls = max(
                self.factory.max_active_calls,
                self.factory.active_calls,
            )
        time.sleep(0.01)
        with self.factory.probe_lock:
            self.factory.active_calls -= 1
        return "ok"

    def refresh_probe(self, label: str) -> str:
        self.factory.remote_calls += 1
        self.client.label = label
        callback = self.factory.during_refresh
        self.factory.during_refresh = None
        if callback is not None:
            callback()
        return "refreshed"

    def account_probe(self) -> str:
        self.factory.remote_calls += 1
        return self.client.label

    def failing_refresh_probe(self, label: str) -> None:
        self.client.label = label
        raise GarminConnectConnectionError("remote operation failed")


class FakeGarminFactory:
    def __init__(self):
        self.instances: list[FakeGarmin] = []
        self.login_error: Exception | None = None
        self.bootstrap_label = "new"
        self.probe_lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0
        self.remote_calls = 0
        self.after_runtime_login: Callable[[], None] | None = None
        self.during_refresh: Callable[[], None] | None = None

    def __call__(self, **kwargs: Any) -> FakeGarmin:
        instance = FakeGarmin(self, kwargs)
        self.instances.append(instance)
        return instance


def build_manager(store: TokenStore, factory: FakeGarminFactory) -> GarminSessionManager:
    config = GarminConfig(garmintokens=str(store.directory))
    return GarminSessionManager(lambda: config, factory)


def test_runtime_loads_tokens_inline_without_dependency_disk_writer(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("valid"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)

    first = manager.get_client()
    second = manager.get_client()

    assert first is second
    assert len(factory.instances) == 1
    assert factory.instances[0].kwargs == {}
    login_input = factory.instances[0].login_inputs[0]
    assert len(login_input) > 512
    assert not Path(login_input).exists()
    assert json.loads(login_input)["di_refresh_token"] == "refresh-valid"


def test_locked_dependency_runtime_inline_branch_never_calls_dump(monkeypatch):
    garmin = Garmin()
    monkeypatch.setattr(garmin.client, "_token_expires_soon", lambda: False)
    monkeypatch.setattr(garmin, "_load_profile_and_settings", lambda: None)

    def forbidden_dump(_path):
        pytest.fail("garminconnect attempted a filesystem token dump")

    monkeypatch.setattr(garmin.client, "dump", forbidden_dump)

    garmin.login(_inline_token_payload(token_payload("inline")))

    assert garmin.client.di_refresh_token == "refresh-inline"
    assert garmin.client._tokenstore_path is None


def test_locked_dependency_bootstrap_inline_branch_never_calls_dump(monkeypatch):
    garmin = Garmin(email="user@example.com", password="fake-password")

    def fake_credential_login(*_args, **_kwargs):
        garmin.client.loads(token_payload("bootstrap"))
        return None, None

    def forbidden_dump(_path):
        pytest.fail("garminconnect attempted a filesystem token dump")

    monkeypatch.setattr(garmin.client, "login", fake_credential_login)
    monkeypatch.setattr(garmin.client, "dump", forbidden_dump)
    monkeypatch.setattr(garmin, "_load_profile_and_settings", lambda: None)

    garmin.login(_invalid_inline_token_payload())

    assert garmin.client.di_refresh_token == "refresh-bootstrap"
    assert garmin.client._tokenstore_path is None


def test_runtime_reloads_when_external_process_replaces_generation(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("first"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    first = manager.get_client()

    store.replace_payload(token_payload("external"))
    second = manager.get_client()

    assert second is not first
    assert len(factory.instances) == 2
    assert factory.instances[1].client.label == "external"


def test_runtime_retries_if_generation_changes_during_unchanged_login(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("first"))
    factory = FakeGarminFactory()

    def replace_during_login() -> None:
        store.replace_payload(token_payload("external"))

    factory.after_runtime_login = replace_during_login
    manager = build_manager(store, factory)

    wrapper = manager.get_client()

    assert len(factory.instances) == 2
    assert factory.instances[0].client.label == "first"
    assert factory.instances[1].client.label == "external"
    assert wrapper.safe_call("account_probe") == "external"


def test_runtime_does_not_fall_back_when_token_is_missing(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)

    with pytest.raises(GarminAuthenticationError, match="No secure, valid Garmin token store"):
        manager.get_client()

    assert factory.instances == []


def test_shared_wrapper_serializes_concurrent_client_calls(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("valid"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(lambda _index: wrapper.safe_call("concurrency_probe"), range(8))
        )

    assert results == ["ok"] * 8
    assert factory.max_active_calls == 1


def test_runtime_refresh_is_persisted_through_atomic_canonical_writer(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    result = wrapper.safe_call("refresh_probe", "refreshed")

    assert result == "refreshed"
    assert json.loads(store.read_snapshot().payload)["di_refresh_token"] == "refresh-refreshed"


def test_generation_replacement_between_calls_revokes_wrapper_before_network(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    assert wrapper.safe_call("account_probe") == "old"
    store.replace_payload(token_payload("external"))

    with pytest.raises(GarminAuthenticationError, match="changed in another process"):
        wrapper.safe_call("account_probe")
    with pytest.raises(GarminAuthenticationError, match="revoked"):
        wrapper.safe_call("account_probe")
    assert factory.remote_calls == 1


def test_successful_mutation_is_not_masked_by_mid_call_generation_conflict(
    tmp_path: Path,
    caplog,
):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    stale_wrapper = manager.get_client()

    def replace_during_call() -> None:
        store.replace_payload(token_payload("external"))

    factory.during_refresh = replace_during_call

    result = stale_wrapper.safe_call("refresh_probe", "stale")

    assert result == "refreshed"
    assert json.loads(store.read_snapshot().payload)["di_refresh_token"] == "refresh-external"
    assert manager.get_client() is not stale_wrapper
    assert "persistence failed" in caplog.text
    with pytest.raises(GarminAuthenticationError, match="revoked"):
        stale_wrapper.safe_call("account_probe")
    assert factory.remote_calls == 1


def test_successful_mutation_is_not_masked_by_persistence_io_failure(
    monkeypatch, tmp_path: Path, caplog
):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    def fail_persistence(_self, _payload, expected_fingerprint):
        assert expected_fingerprint
        raise TokenStoreError("simulated disk failure")

    monkeypatch.setattr(TokenStore, "compare_and_replace", fail_persistence)

    result = wrapper.safe_call("refresh_probe", "remote-success")

    assert result == "refreshed"
    assert "persistence failed" in caplog.text
    assert json.loads(store.read_snapshot().payload)["di_refresh_token"] == "refresh-old"
    with pytest.raises(GarminAuthenticationError, match="revoked"):
        wrapper.safe_call("account_probe")
    assert factory.remote_calls == 1


def test_persistence_failure_does_not_mask_original_api_error(monkeypatch, tmp_path: Path, caplog):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    def fail_persistence(_self, _payload, expected_fingerprint):
        assert expected_fingerprint
        raise TokenStoreError("simulated disk failure")

    monkeypatch.setattr(TokenStore, "compare_and_replace", fail_persistence)

    with pytest.raises(GarminAPIError, match="remote operation failed"):
        wrapper.safe_call("failing_refresh_probe", "refreshed-before-error")

    assert "persistence failed" in caplog.text


def test_unexpected_persistence_failure_revokes_before_next_network_call(
    monkeypatch,
    tmp_path: Path,
    caplog,
):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)
    wrapper = manager.get_client()

    def fail_dump() -> str:
        raise ValueError("unexpected serialization failure")

    monkeypatch.setattr(factory.instances[0].client, "dumps", fail_dump)

    assert wrapper.safe_call("account_probe") == "old"
    assert "unexpected serialization failure" in caplog.text
    with pytest.raises(GarminAuthenticationError, match="revoked"):
        wrapper.safe_call("account_probe")
    assert factory.remote_calls == 1
    assert manager.get_client() is not wrapper


def test_bootstrap_atomically_commits_ephemeral_credentials(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    manager = build_manager(store, factory)

    returned_store = manager.authenticate("user@example.com", "secret", lambda: "123456")

    assert returned_store.directory == store.directory
    assert json.loads(store.read_snapshot().payload)["di_refresh_token"] == "refresh-new"
    instance = factory.instances[0]
    assert instance.kwargs["email"] == "user@example.com"
    assert instance.kwargs["password"] == "secret"
    assert len(instance.login_inputs[0]) > 512
    assert not list(store.directory.glob(".auth-*"))


def test_failed_bootstrap_preserves_existing_token(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    factory = FakeGarminFactory()
    factory.login_error = GarminConnectAuthenticationError("bad credentials")
    manager = build_manager(store, factory)

    with pytest.raises(GarminAuthenticationError):
        manager.authenticate("user@example.com", "wrong", lambda: "123456")

    assert json.loads(store.read_snapshot().payload)["di_refresh_token"] == "refresh-old"
