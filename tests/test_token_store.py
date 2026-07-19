"""Tests for secure atomic canonical token storage."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from typing import Any

import pytest
from garminconnect import Garmin

import garmin_connect_mcp.token_store as token_store_module
from garmin_connect_mcp.token_store import (
    LOCK_FILENAME,
    TokenStore,
    TokenStoreConflict,
    TokenStoreError,
)
from tests.conftest import trust_windows_test_parent


def token_payload(label: str) -> str:
    return json.dumps(
        {
            "di_token": f"access-{label}",
            "di_refresh_token": f"refresh-{label}",
            "di_client_id": f"client-{label}",
        }
    )


def read_label(store: TokenStore) -> str:
    payload = json.loads(store.read_snapshot().payload)
    return payload["di_refresh_token"]


def protect_interrupted(path: Path) -> None:
    """Match the protection applied before a writer marks a temp durable."""
    TokenStore._harden_path(path, directory=False)


def compare_and_replace_worker(
    directory: str,
    expected_fingerprint: str,
    label: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Race a generation commit from a spawned process."""
    trust_windows_test_parent(Path(directory).parent)
    store = TokenStore(Path(directory))
    ready.put(label)
    start.wait(10)
    try:
        store.compare_and_replace(token_payload(label), expected_fingerprint)
    except TokenStoreConflict:
        results.put("conflict")
    else:
        results.put("committed")


def paused_first_writer_worker(
    directory: str,
    ready: Any,
    release: Any,
    results: Any,
) -> None:
    """Pause a spawned upgrade writer after it creates the first lock and temp."""
    trust_windows_test_parent(Path(directory).parent)
    real_atomic_replace = token_store_module.atomic_replace_file

    def paused_atomic_replace(source: Path, target: Path) -> None:
        ready.set()
        release.wait(10)
        real_atomic_replace(source, target)

    token_store_module.atomic_replace_file = paused_atomic_replace
    try:
        TokenStore(Path(directory)).replace_payload(token_payload("new"))
    except Exception as exc:  # pragma: no cover - only reported to the parent
        results.put(f"error:{type(exc).__name__}:{exc}")
    else:
        results.put("committed")


def hold_windows_shared_reader(path: str, ready: Any, release: Any) -> None:
    """Hold the canonical file open from another Windows process."""
    from garmin_connect_mcp.windows_io import open_shared_read

    descriptor = open_shared_read(Path(path))
    try:
        ready.set()
        release.wait(10)
    finally:
        os.close(descriptor)


def test_atomic_replace_commits_one_canonical_token(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))

    committed = store.replace_payload(token_payload("new"))

    assert committed == store.read_snapshot()
    assert read_label(store) == "refresh-new"
    assert list(store.directory.glob(".token-*.tmp")) == []


def test_writer_reconciles_owned_single_link_crash_temp(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    stale = store.directory / ".token-crash.tmp"
    stale.write_text(token_payload("stale-copy"), encoding="utf-8")
    protect_interrupted(stale)

    store.replace_payload(token_payload("new"))

    assert not stale.exists()
    assert list(store.directory.glob(".token-*.tmp")) == []
    assert read_label(store) == "refresh-new"
    assert store.permissions_secure() is True


def test_reader_preserves_owned_single_link_crash_temp(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    stale = store.directory / ".token-crash.tmp"
    stale.write_text(token_payload("stale-copy"), encoding="utf-8")
    protect_interrupted(stale)

    snapshot = store.read_snapshot()

    assert json.loads(snapshot.payload)["di_refresh_token"] == "refresh-old"
    assert stale.exists()
    assert store.interrupted_writes() == (stale,)
    assert store.permissions_secure() is True


def test_reader_waits_for_live_writer_temp(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    writer_at_replace = threading.Event()
    reader_started = threading.Event()
    release_writer = threading.Event()
    real_atomic_replace = token_store_module.atomic_replace_file

    def paused_atomic_replace(source: Path, target: Path) -> None:
        writer_at_replace.set()
        if not release_writer.wait(timeout=5):
            raise AssertionError("Reader/writer coordination test timed out")
        real_atomic_replace(source, target)

    def read_during_write():
        reader_started.set()
        return store.read_snapshot()

    monkeypatch.setattr(token_store_module, "atomic_replace_file", paused_atomic_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(store.replace_payload, token_payload("new"))
        try:
            assert writer_at_replace.wait(timeout=5)
            reader = executor.submit(read_during_write)
            assert reader_started.wait(timeout=5)
            assert not reader.done()
        finally:
            release_writer.set()

        writer.result(timeout=5)
        snapshot = reader.result(timeout=5)

    assert json.loads(snapshot.payload)["di_refresh_token"] == "refresh-new"
    assert list(store.directory.glob(".token-*.tmp")) == []


def test_upgraded_reader_waits_for_first_writer_across_processes(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    (store.directory / LOCK_FILENAME).unlink()
    context = multiprocessing.get_context("spawn")
    writer_ready = context.Event()
    release_writer = context.Event()
    results = context.Queue()
    process = context.Process(
        target=paused_first_writer_worker,
        args=(str(store.directory), writer_ready, release_writer, results),
    )
    reader_started = threading.Event()

    def read_during_first_write():
        reader_started.set()
        return store.read_snapshot()

    process.start()
    try:
        assert writer_ready.wait(timeout=10)
        with ThreadPoolExecutor(max_workers=1) as executor:
            reader = executor.submit(read_during_first_write)
            assert reader_started.wait(timeout=5)
            assert not reader.done()
            release_writer.set()
            snapshot = reader.result(timeout=10)
        assert results.get(timeout=10) == "committed"
    finally:
        release_writer.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)

    assert process.exitcode == 0
    assert json.loads(snapshot.payload)["di_refresh_token"] == "refresh-new"
    assert list(store.directory.glob(".token-*.tmp")) == []


def test_conflicting_writer_preserves_interrupted_candidate(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    interrupted = store.directory / ".token-crash.tmp"
    interrupted.write_text(token_payload("possibly-newer"), encoding="utf-8")
    protect_interrupted(interrupted)

    with pytest.raises(TokenStoreConflict):
        store.compare_and_replace(token_payload("replacement"), "stale-fingerprint")

    assert interrupted.exists()
    assert store.interrupted_writes() == (interrupted,)


def test_writer_refuses_temporary_token_directory_lookalike(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    lookalike = store.directory / ".token-crash.tmp"
    lookalike.mkdir()

    with pytest.raises(TokenStoreError, match="single-link regular non-redirect"):
        store.replace_payload(token_payload("new"))

    assert lookalike.is_dir()
    assert json.loads(store.token_file.read_text(encoding="utf-8"))["di_refresh_token"] == (
        "refresh-old"
    )


def test_writer_refuses_hardlinked_canonical_token(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    external_copy = tmp_path / "external-token-link.json"
    try:
        os.link(store.token_file, external_copy)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")

    assert store.permissions_secure() is False
    with pytest.raises(TokenStoreError, match="single-link regular non-redirect"):
        store.replace_payload(token_payload("new"))

    assert json.loads(external_copy.read_text(encoding="utf-8"))["di_refresh_token"] == (
        "refresh-old"
    )


def test_writer_refuses_hardlinked_crash_temp(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    stale = store.directory / ".token-crash.tmp"
    stale.write_text(token_payload("stale-copy"), encoding="utf-8")
    external_copy = tmp_path / "external-temp-link.json"
    try:
        os.link(stale, external_copy)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")

    with pytest.raises(TokenStoreError, match="single-link regular non-redirect"):
        store.replace_payload(token_payload("new"))

    assert stale.exists()
    assert external_copy.exists()
    assert json.loads(store.token_file.read_text(encoding="utf-8"))["di_refresh_token"] == (
        "refresh-old"
    )


def test_atomic_replace_failure_preserves_existing_token_and_durable_candidate(
    monkeypatch,
    tmp_path: Path,
):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))

    def fail_replace(_source, _target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(token_store_module, "atomic_replace_file", fail_replace)

    with pytest.raises(OSError, match="replace failure"):
        store.replace_payload(token_payload("new"))

    assert read_label(store) == "refresh-old"
    interrupted = store.interrupted_writes()
    assert len(interrupted) == 1
    candidate = TokenStore.read_external_artifact(interrupted[0])
    assert json.loads(candidate.payload)["di_refresh_token"] == "refresh-new"


def test_post_commit_directory_sync_failure_does_not_report_failure(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    sync_calls = 0

    def fail_sync():
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 2:
            raise OSError("simulated post-commit directory sync failure")

    monkeypatch.setattr(store, "_sync_directory", fail_sync)

    committed = store.replace_payload(token_payload("new"))

    assert committed == store.read_snapshot()


def test_staging_sync_failure_keeps_older_interrupted_generation(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    interrupted = store.directory / ".token-older.tmp"
    interrupted.write_text(token_payload("possibly-newer"), encoding="utf-8")
    protect_interrupted(interrupted)

    def fail_sync():
        raise OSError("simulated staged-entry sync failure")

    monkeypatch.setattr(store, "_sync_directory", fail_sync)

    with pytest.raises(OSError, match="staged-entry sync failure"):
        store.replace_payload(token_payload("replacement"))

    assert read_label(store) == "refresh-old"
    assert store.interrupted_writes() == (interrupted,)


def test_writer_syncs_directory_after_discarding_interrupted_generation(
    monkeypatch,
    tmp_path: Path,
):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    interrupted = store.directory / ".token-older.tmp"
    interrupted.write_text(token_payload("possibly-newer"), encoding="utf-8")
    protect_interrupted(interrupted)
    sync_calls = 0

    def record_sync():
        nonlocal sync_calls
        sync_calls += 1

    monkeypatch.setattr(store, "_sync_directory", record_sync)

    store.replace_payload(token_payload("replacement"))

    assert sync_calls == 3
    assert not interrupted.exists()


def test_unsafe_interrupted_artifact_fails_store_security(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    interrupted = store.directory / ".token-unsafe.tmp"
    interrupted.write_text(token_payload("possibly-exposed"), encoding="utf-8")
    protect_interrupted(interrupted)
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(interrupted), "/grant", "*S-1-1-0:(R)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("Could not create an unsafe interrupted-token ACL fixture")
    else:
        interrupted.chmod(0o644)

    assert store.permissions_secure() is False
    with pytest.raises(TokenStoreError, match="may have exposed secrets"):
        store.interrupted_writes()
    assert interrupted.exists()


def test_compare_and_replace_rejects_stale_generation(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    old = store.replace_payload(token_payload("old"))
    store.replace_payload(token_payload("external"))

    with pytest.raises(TokenStoreConflict, match="changed in another process"):
        store.compare_and_replace(token_payload("stale"), old.fingerprint)

    assert read_label(store) == "refresh-external"


def test_two_processes_cannot_commit_the_same_generation(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    original = store.replace_payload(token_payload("original"))
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=compare_and_replace_worker,
            args=(
                str(store.directory),
                original.fingerprint,
                label,
                ready,
                start,
                results,
            ),
        )
        for label in ("first", "second")
    ]
    for process in processes:
        process.start()
    try:
        assert {ready.get(timeout=10), ready.get(timeout=10)} == {"first", "second"}
        start.set()
        outcomes = sorted([results.get(timeout=10), results.get(timeout=10)])
    except Empty as exc:
        pytest.fail(f"Spawned token-store race timed out: {exc}")
    finally:
        start.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert outcomes == ["committed", "conflict"]
    assert read_label(store) in {"refresh-first", "refresh-second"}
    assert all(process.exitcode == 0 for process in processes)


@pytest.mark.parametrize(
    "payload",
    [
        "{}",
        json.dumps({"di_refresh_token": "refresh-only"}),
        json.dumps({"di_token": "access", "di_refresh_token": 123, "di_client_id": "client"}),
    ],
)
def test_validator_rejects_incomplete_or_wrong_type_payload(payload: str):
    with pytest.raises(TokenStoreError, match="required 0.3 token fields"):
        TokenStore.validate_payload(payload)


def test_validator_is_compatible_with_real_garminconnect_loader(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    snapshot = store.replace_payload(token_payload("real-loader"))
    client = Garmin().client

    client.loads(snapshot.payload)

    assert client.di_token == "access-real-loader"
    assert client.di_refresh_token == "refresh-real-loader"
    assert client.di_client_id == "client-real-loader"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_runtime_rejects_insecure_existing_permissions(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("new"))
    store.token_file.chmod(0o644)

    assert store.exists() is True
    assert store.permissions_secure() is False
    with pytest.raises(TokenStoreError, match="permissions or ownership are unsafe"):
        store.read_snapshot()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
def test_commit_enforces_owner_only_permissions(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")

    store.replace_payload(token_payload("new"))

    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.token_file.stat().st_mode) == 0o600
    assert store.permissions_secure() is True


def test_writer_refuses_empty_or_broad_store_location():
    for configured in ("", Path.cwd(), Path.home(), Path(Path.cwd().anchor)):
        store = TokenStore(configured)
        with pytest.raises(TokenStoreError, match="dedicated token-store directory"):
            store.validate_dedicated_location()


def test_writer_refuses_non_dedicated_directory_without_changing_protection(tmp_path: Path):
    directory = tmp_path / "existing-project"
    directory.mkdir()
    unrelated = directory / "unrelated.txt"
    unrelated.write_text("keep me", encoding="utf-8")
    if os.name == "nt":
        from garmin_connect_mcp.windows_acl import read_acl_descriptor

        before_protection = read_acl_descriptor(directory)
    else:
        directory.chmod(0o750)
        before_protection = stat.S_IMODE(directory.stat().st_mode)

    store = TokenStore(directory)
    with pytest.raises(TokenStoreError, match="unrelated entries"):
        store.replace_payload(token_payload("new"))

    if os.name == "nt":
        assert read_acl_descriptor(directory) == before_protection
    else:
        assert stat.S_IMODE(directory.stat().st_mode) == before_protection
    assert unrelated.read_text(encoding="utf-8") == "keep me"
    assert not store.token_file.exists()


def test_writer_refuses_insecure_existing_empty_directory_without_repair(tmp_path: Path):
    directory = tmp_path / "existing-token-directory"
    directory.mkdir()
    if os.name == "nt":
        from garmin_connect_mcp.windows_acl import (
            acl_is_owner_only,
            read_acl_descriptor,
        )

        if acl_is_owner_only(directory):
            result = subprocess.run(
                ["icacls", str(directory), "/grant", "*S-1-1-0:(R)"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                pytest.skip("icacls could not create the insecure ACL test fixture")
        before_protection = read_acl_descriptor(directory)
    else:
        directory.chmod(0o755)
        before_protection = stat.S_IMODE(directory.stat().st_mode)

    store = TokenStore(directory)
    with pytest.raises(TokenStoreError, match="must already be owner-only"):
        store.replace_payload(token_payload("new"))

    if os.name == "nt":
        assert read_acl_descriptor(directory) == before_protection
    else:
        assert stat.S_IMODE(directory.stat().st_mode) == before_protection
    assert not store.token_file.exists()


def test_writer_requires_an_existing_verified_parent(tmp_path: Path):
    missing_parent = tmp_path / "missing-parent"
    store = TokenStore(missing_parent / "tokens")

    with pytest.raises((OSError, TokenStoreError), match="integrity boundary|does not exist"):
        store.replace_payload(token_payload("new"))

    assert not missing_parent.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-entry integrity contract")
def test_existing_store_is_rejected_after_parent_becomes_shared_writable(tmp_path: Path):
    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir(mode=0o700)
    store = TokenStore(shared_parent / "tokens")
    store.replace_payload(token_payload("old"))
    shared_parent.chmod(0o777)

    with pytest.raises(TokenStoreError, match="writable without sticky protection"):
        store.read_snapshot()

    assert store.token_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory durability contract")
def test_first_store_creation_fsyncs_directory_inode_then_parent(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    real_fsync = token_store_module.os.fsync
    synced_inodes: list[int] = []

    def record_fsync(descriptor: int) -> None:
        synced_inodes.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(token_store_module.os, "fsync", record_fsync)

    store.ensure_directory()

    assert synced_inodes == [store.directory.stat().st_ino, tmp_path.stat().st_ino]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission durability contract")
def test_permission_repair_fchmod_fsync_order(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    store.directory.chmod(0o755)
    store.token_file.chmod(0o644)
    real_fchmod = token_store_module.os.fchmod  # pyright: ignore[reportAttributeAccessIssue]
    real_fsync = token_store_module.os.fsync
    events: list[tuple[str, int]] = []

    def record_fchmod(descriptor: int, mode: int) -> None:
        events.append(("fchmod", mode))
        real_fchmod(descriptor, mode)

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", stat.S_IMODE(os.fstat(descriptor).st_mode)))
        real_fsync(descriptor)

    monkeypatch.setattr(token_store_module.os, "fchmod", record_fchmod)
    monkeypatch.setattr(token_store_module.os, "fsync", record_fsync)

    store.enforce_permissions()

    assert events == [
        ("fchmod", 0o700),
        ("fsync", 0o700),
        ("fchmod", 0o600),
        ("fsync", 0o600),
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission durability contract")
def test_permission_repair_reports_inode_fsync_failure(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    store.directory.chmod(0o755)

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("simulated permission durability failure")

    monkeypatch.setattr(token_store_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="permission durability failure"):
        store.enforce_permissions()


def test_writer_creates_secure_child_beneath_preexisting_parent(tmp_path: Path):
    volume_root = tmp_path / "docker-volume-root"
    volume_root.mkdir()
    if os.name != "nt":
        volume_root.chmod(0o755)
    store = TokenStore(volume_root / "tokens")

    store.replace_payload(token_payload("new"))

    assert store.permissions_secure() is True
    if os.name != "nt":
        assert stat.S_IMODE(volume_root.stat().st_mode) == 0o755
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


def test_permission_repair_preflights_all_owners_before_any_hardening(
    monkeypatch,
    tmp_path: Path,
):
    store = TokenStore(tmp_path / "tokens")
    store.directory.mkdir()
    store.token_file.write_text(token_payload("legacy"), encoding="utf-8")
    checked: list[Path] = []
    hardened: list[Path] = []

    def reject_foreign_token(_cls, path: Path) -> None:
        checked.append(path)
        if path == store.token_file:
            raise TokenStoreError(f"foreign owner: {path}")

    def record_hardening(_cls, path: Path, *, directory: bool) -> None:
        del directory
        hardened.append(path)

    monkeypatch.setattr(
        TokenStore,
        "_assert_current_owner",
        classmethod(reject_foreign_token),
    )
    monkeypatch.setattr(TokenStore, "_harden_path", classmethod(record_hardening))

    with pytest.raises(TokenStoreError, match="foreign owner"):
        store.enforce_permissions()

    assert checked == [store.directory, store.token_file]
    assert hardened == []


def test_existing_foreign_lock_is_rejected_before_write_or_chmod(monkeypatch, tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.directory.mkdir()
    lock_path = store.directory / LOCK_FILENAME
    lock_path.touch()
    writes: list[bytes] = []
    hardening: list[Path] = []

    def reject_owner(_cls, path: Path) -> None:
        raise TokenStoreError(f"foreign owner: {path}")

    def record_write(_descriptor: int, content: bytes) -> int:
        writes.append(content)
        return len(content)

    def record_hardening(_cls, path: Path, *, directory: bool) -> None:
        del directory
        hardening.append(path)

    monkeypatch.setattr(TokenStore, "_assert_current_owner", classmethod(reject_owner))
    monkeypatch.setattr(TokenStore, "_harden_path", classmethod(record_hardening))
    monkeypatch.setattr(token_store_module.os, "write", record_write)

    with pytest.raises(TokenStoreError, match="foreign owner"):
        with store._exclusive_file_lock(lock_path, "test"):
            pytest.fail("foreign lock must never be acquired")

    assert writes == []
    assert hardening == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership ordering contract")
def test_root_hardening_rejects_foreign_legacy_before_open_or_fchmod(
    monkeypatch,
    tmp_path: Path,
):
    legacy = tmp_path / "legacy-token"
    snapshot = TokenStore.validate_payload(token_payload("legacy"))
    mutations: list[str] = []
    foreign_metadata = SimpleNamespace(st_uid=1234)
    monkeypatch.setattr(
        TokenStore,
        "read_external_artifact",
        classmethod(lambda _cls, _path: snapshot),
    )
    monkeypatch.setattr(
        TokenStore,
        "_assert_regular_non_redirect",
        classmethod(lambda _cls, _path: foreign_metadata),
    )
    monkeypatch.setattr(token_store_module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        token_store_module.os,
        "open",
        lambda *_args, **_kwargs: mutations.append("open"),
    )
    monkeypatch.setattr(
        token_store_module.os,
        "fchmod",
        lambda *_args: mutations.append("fchmod"),
    )

    with pytest.raises(TokenStoreError, match="owned by another user"):
        TokenStore.harden_external_artifact(legacy, snapshot.fingerprint)

    assert mutations == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode durability contract")
def test_external_hardening_fsyncs_verified_mode_after_fchmod(monkeypatch, tmp_path: Path):
    legacy = tmp_path / "legacy-token"
    legacy.write_text(token_payload("legacy"), encoding="utf-8")
    legacy.chmod(0o644)
    fingerprint = TokenStore.read_external_artifact(legacy).fingerprint
    real_fchmod = token_store_module.os.fchmod  # pyright: ignore[reportAttributeAccessIssue]
    real_fsync = token_store_module.os.fsync
    events: list[str] = []

    def record_fchmod(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        events.append("fchmod")

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_nlink == 1
        events.append("fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(token_store_module.os, "fchmod", record_fchmod)
    monkeypatch.setattr(token_store_module.os, "fsync", record_fsync)

    TokenStore.harden_external_artifact(legacy, fingerprint)

    assert events == ["fchmod", "fsync"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX inode durability contract")
def test_external_hardening_fsync_failure_is_reported(monkeypatch, tmp_path: Path):
    legacy = tmp_path / "legacy-token"
    legacy.write_text(token_payload("legacy"), encoding="utf-8")
    legacy.chmod(0o644)
    fingerprint = TokenStore.read_external_artifact(legacy).fingerprint

    def fail_fsync(descriptor: int) -> None:
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
        raise OSError("simulated inode durability failure")

    monkeypatch.setattr(token_store_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="inode durability failure"):
        TokenStore.harden_external_artifact(legacy, fingerprint)

    assert legacy.exists()
    assert stat.S_IMODE(legacy.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific ACL contract")
def test_windows_store_enforces_and_verifies_owner_only_acl(tmp_path: Path):
    from garmin_connect_mcp.windows_acl import acl_is_owner_only

    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("new"))

    assert acl_is_owner_only(store.directory)
    assert acl_is_owner_only(store.token_file)
    assert store.permissions_secure() is True


@pytest.mark.skipif(os.name != "nt", reason="Windows parent-entry integrity contract")
def test_windows_parent_mutation_rights_block_store_creation(tmp_path: Path):
    shared_parent = tmp_path / "shared-parent"
    shared_parent.mkdir()
    result = subprocess.run(
        ["icacls", str(shared_parent), "/grant", "*S-1-1-0:(OI)(CI)(M)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("icacls could not create the hostile parent ACL fixture")
    store = TokenStore(shared_parent / "tokens")

    with pytest.raises(TokenStoreError, match="parent grants untrusted mutation rights"):
        store.replace_payload(token_payload("new"))

    assert not store.directory.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows SDDL rights contract")
@pytest.mark.parametrize(
    ("rights", "dangerous"),
    (
        ("0x40", True),
        ("0x10000", True),
        ("0x1200a9", False),
        ("SD", True),
        ("FA", True),
        ("FRFX", False),
    ),
)
def test_windows_directory_rights_parser(rights: str, dangerous: bool):
    from garmin_connect_mcp.windows_acl import _rights_allow_directory_mutation

    assert _rights_allow_directory_mutation(rights) is dangerous


@pytest.mark.skipif(os.name != "nt", reason="Windows NULL DACL contract")
def test_windows_integrity_parser_rejects_null_dacl(monkeypatch, tmp_path: Path):
    import garmin_connect_mcp.windows_acl as windows_acl

    current_sid = "S-1-5-21-test-user"
    descriptor = windows_acl.WindowsAclDescriptor(
        owner_sid=current_sid,
        dacl="D:NO_ACCESS_CONTROL",
    )
    monkeypatch.setattr(windows_acl, "read_acl_descriptor", lambda _path: descriptor)
    monkeypatch.setattr(windows_acl, "_current_user_sid", lambda: current_sid)

    assert (
        windows_acl.directory_entry_integrity_is_protected(
            tmp_path,
            require_current_owner=True,
        )
        is False
    )
    assert windows_acl.file_integrity_is_protected(tmp_path / "artifact") is False


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific ACL contract")
def test_windows_runtime_rejects_acl_that_grants_everyone_read(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("new"))
    result = subprocess.run(
        ["icacls", str(store.token_file), "/grant", "*S-1-1-0:(R)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("icacls could not create the insecure ACL test fixture")

    assert store.permissions_secure() is False
    with pytest.raises(TokenStoreError, match="permissions or ownership are unsafe"):
        store.read_snapshot()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL replacement contract")
def test_windows_writer_hardens_old_target_before_replace(monkeypatch, tmp_path: Path):
    from garmin_connect_mcp.windows_acl import acl_is_owner_only

    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    result = subprocess.run(
        ["icacls", str(store.token_file), "/grant", "*S-1-1-0:(R)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("icacls could not create the insecure ACL test fixture")
    real_replace = token_store_module.atomic_replace_file
    target_was_secure = False

    def observe_replace(source: Path, target: Path) -> None:
        nonlocal target_was_secure
        target_was_secure = acl_is_owner_only(target)
        real_replace(source, target)

    monkeypatch.setattr(token_store_module, "atomic_replace_file", observe_replace)

    store.replace_payload(token_payload("new"))

    assert target_was_secure is True
    assert store.permissions_secure() is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_windows_runtime_refuses_directory_junction(tmp_path: Path):
    real_directory = tmp_path / "real-junction-target"
    store = TokenStore(real_directory)
    store.replace_payload(token_payload("real"))
    junction = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(real_directory)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("Junction creation is unavailable on this Windows host")
    try:
        with pytest.raises(TokenStoreError, match="Redirected paths are forbidden"):
            TokenStore(junction).read_snapshot()
    finally:
        junction.rmdir()


@pytest.mark.skipif(os.name != "nt", reason="Windows shared-delete contract")
def test_windows_interprocess_reader_does_not_block_atomic_replace(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(token_payload("old"))
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    reader = context.Process(
        target=hold_windows_shared_reader,
        args=(str(store.token_file), ready, release),
    )
    reader.start()
    try:
        assert ready.wait(10)
        store.replace_payload(token_payload("new"))
    finally:
        release.set()
        reader.join(timeout=10)
        if reader.is_alive():
            reader.terminate()
            reader.join(timeout=5)

    assert reader.exitcode == 0
    assert read_label(store) == "refresh-new"


def test_runtime_refuses_redirected_token_directory(tmp_path: Path):
    real_directory = tmp_path / "real"
    store = TokenStore(real_directory)
    store.replace_payload(token_payload("real"))
    redirected_directory = tmp_path / "redirected"
    try:
        redirected_directory.symlink_to(real_directory, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink creation is unavailable on this platform")

    redirected_store = TokenStore(redirected_directory)
    with pytest.raises(TokenStoreError, match="Redirected paths are forbidden"):
        redirected_store.read_snapshot()


def test_commit_refuses_preexisting_token_symlink(tmp_path: Path):
    store = TokenStore(tmp_path / "tokens")
    store.ensure_directory()
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text(token_payload("unrelated"), encoding="utf-8")
    try:
        store.token_file.symlink_to(unrelated)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")

    with pytest.raises(TokenStoreError, match="regular non-redirect"):
        store.replace_payload(token_payload("new"))

    assert json.loads(unrelated.read_text(encoding="utf-8"))["di_refresh_token"] == (
        "refresh-unrelated"
    )
