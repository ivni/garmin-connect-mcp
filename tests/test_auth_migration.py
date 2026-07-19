"""Tests for planned, recoverable legacy-auth migration."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

import garmin_connect_mcp.auth as auth_module
import garmin_connect_mcp.auth_migration as migration_module
from garmin_connect_mcp.auth import GarminConfig
from garmin_connect_mcp.auth_migration import (
    FileProtection,
    MigrationPlan,
    apply_migration_plan,
    audit_auth_state,
    build_migration_plan,
    migrate_auth_state,
)
from garmin_connect_mcp.token_store import TokenStore, TokenStoreError
from tests.conftest import trust_windows_test_parent


def token_payload(label: str) -> str:
    return json.dumps(
        {
            "di_token": f"access-{label}",
            "di_refresh_token": f"refresh-{label}",
            "di_client_id": f"client-{label}",
        }
    )


def write_token(directory: Path, label: str = "canonical") -> TokenStore:
    store = TokenStore(directory)
    store.replace_payload(token_payload(label))
    return store


def write_legacy(path: Path, label: str = "legacy") -> None:
    path.write_text(token_payload(label), encoding="utf-8")


def grant_untrusted_read(path: Path) -> None:
    """Make a secret artifact observably non-owner-only on this platform."""
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(path), "/grant", "*S-1-1-0:(R)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("icacls could not create the insecure secret ACL fixture")
    else:
        path.chmod(0o644)


def configure_env_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    default_env = tmp_path / ".garminconnect.env"
    default_legacy = tmp_path / ".garminconnect_base64"
    monkeypatch.setattr(auth_module, "DEFAULT_ENV_FILE", default_env)
    monkeypatch.setattr(auth_module, "DEFAULT_LEGACY_TOKEN_FILE", default_legacy)
    monkeypatch.setattr(migration_module, "DEFAULT_LEGACY_TOKEN_FILE", default_legacy)
    monkeypatch.setattr(
        migration_module,
        "DEFAULT_MIGRATION_LOCK_FILE",
        tmp_path / ".auth-migration.lock",
    )
    monkeypatch.chdir(tmp_path)
    for key in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS_BASE64"):
        monkeypatch.delenv(key, raising=False)
    return default_env


def apply_plan_worker(
    plan: MigrationPlan,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    """Apply one prepared plan from a spawned process."""
    trust_windows_test_parent(plan.migration_lock.parent)
    ready.put("ready")
    start.wait(10)
    try:
        apply_migration_plan(plan, lambda _prompt: True)
    except TokenStoreError:
        results.put("stale")
    else:
        results.put("applied")


def crash_during_dotenv_temp_write(
    path: str,
    content: bytes,
    protection: FileProtection,
    expected_fingerprint: str,
) -> None:
    """Simulate process loss immediately after bytes enter the migration temp."""
    trust_windows_test_parent(Path(path).parent)
    real_fdopen = migration_module.os.fdopen

    class CrashAfterWrite:
        def __init__(self, descriptor: int, mode: str):
            self.stream = real_fdopen(descriptor, mode)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name: str):
            return getattr(self.stream, name)

        def write(self, data: bytes):
            self.stream.write(data)
            self.stream.flush()
            os.fsync(self.stream.fileno())
            os._exit(73)

    migration_module.os.fdopen = CrashAfterWrite  # type: ignore[assignment]
    migration_module._atomic_replace_protected_file(
        Path(path),
        content,
        protection,
        expected_fingerprint=expected_fingerprint,
    )


def test_audit_reports_locations_without_secret_values(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text(
        "GARMIN_EMAIL=user@example.com\nGARMIN_PASSWORD=top-secret\n",
        encoding="utf-8",
    )
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path, "sensitive-token")

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)), legacy_path)

    report = " ".join(audit.issues)
    assert audit.legacy_exists is True
    assert audit.stored_credentials == (default_env,)
    assert "top-secret" not in report
    assert "refresh-sensitive-token" not in report
    assert str(default_env) in report


def test_audit_marks_readable_legacy_token_as_possible_disclosure(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)
    grant_untrusted_read(legacy_path)

    audit = audit_auth_state(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
    )

    assert audit.legacy_permissions_secure is False
    assert audit.needs_migration is True
    assert any("may already have exposed" in issue for issue in audit.issues)


def test_migration_repairs_current_owned_insecure_quarantine(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    grant_untrusted_read(quarantine)

    audit = audit_auth_state(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
    )
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
    )

    assert audit.quarantine_permissions_secure is False
    assert plan.repair_quarantine_permissions is True
    result = apply_migration_plan(plan, lambda _prompt: True)
    assert result.repaired_quarantine_permissions is True
    assert TokenStore.external_artifact_permissions_secure(quarantine) is True
    assert quarantine.exists()


def test_purge_refuses_quarantine_protection_changed_after_plan(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    TokenStore._harden_path(quarantine, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
        purge_quarantine=True,
    )
    grant_untrusted_read(quarantine)

    with pytest.raises(TokenStoreError, match="Quarantine protection changed"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert quarantine.exists()


def test_migration_refuses_legacy_protection_changed_after_plan(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)
    TokenStore._harden_path(legacy_path, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )
    grant_untrusted_read(legacy_path)

    with pytest.raises(TokenStoreError, match="Legacy token protection changed"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert legacy_path.exists()


def test_purge_rechecks_quarantine_owner_immediately_before_apply(
    monkeypatch,
    tmp_path: Path,
):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    TokenStore._harden_path(quarantine, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
        purge_quarantine=True,
    )
    real_owner_check = TokenStore._assert_current_owner.__func__
    quarantine_owner_checks = 0

    def reject_quarantine_owner(cls: type[TokenStore], path: Path) -> None:
        nonlocal quarantine_owner_checks
        if Path(os.path.abspath(path)) == quarantine:
            quarantine_owner_checks += 1
            if quarantine_owner_checks == 2:
                raise TokenStoreError(f"Authentication artifact is foreign-owned: {path}")
        real_owner_check(cls, path)

    monkeypatch.setattr(
        TokenStore,
        "_assert_current_owner",
        classmethod(reject_quarantine_owner),
    )

    with pytest.raises(TokenStoreError, match="foreign-owned"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert quarantine_owner_checks == 2
    assert quarantine.exists()


def test_audit_preserves_and_reports_owned_crash_temp(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    stale = store.directory / ".token-crash.tmp"
    stale.write_text(token_payload("stale-copy"), encoding="utf-8")
    TokenStore._harden_path(stale, directory=False)

    audit = audit_auth_state(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "no-legacy-file",
    )
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.token_exists is True
    assert audit.token_permissions_secure is True
    assert audit.interrupted_writes == (stale,)
    assert stale.exists()
    assert any("preserved for recovery" in issue for issue in audit.issues)
    assert any("successful new authentication or refresh" in blocker for blocker in plan.blockers)


def test_migration_requires_confirmation(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)

    result = migrate_auth_state(
        lambda _prompt: False,
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )

    assert result.changed is False
    assert legacy_path.exists()
    assert dotenv_values(default_env)["GARMIN_PASSWORD"] == "secret"


def test_migration_cleans_env_and_quarantines_before_separate_purge(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text(
        "GARMIN_EMAIL=user@example.com\n"
        "GARMIN_PASSWORD=secret\n"
        "GARMINTOKENS_BASE64=/tmp/legacy\n"
        "KEEP=value\n",
        encoding="utf-8",
    )
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)
    synced_directories: list[Path] = []
    monkeypatch.setattr(
        migration_module,
        "_sync_directory",
        lambda path: synced_directories.append(Path(path)),
    )

    result = migrate_auth_state(
        lambda _prompt: True,
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )

    values = dotenv_values(default_env)
    quarantine = TokenStore.quarantine_path_for(store.directory)
    assert result.changed is True
    assert result.quarantined_legacy_token is True
    assert result.quarantine_path == quarantine
    assert not legacy_path.exists()
    assert quarantine.exists()
    assert "GARMIN_EMAIL" not in values
    assert "GARMIN_PASSWORD" not in values
    assert "GARMINTOKENS_BASE64" not in values
    assert values["KEEP"] == "value"
    assert default_env.parent in synced_directories
    assert store.directory in synced_directories

    purge_plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        purge_quarantine=True,
    )
    synced_directories.clear()
    purge_result = apply_migration_plan(purge_plan, lambda _prompt: True)

    assert purge_result.purged_quarantine is True
    assert not quarantine.exists()
    assert synced_directories == [store.directory]


def test_purge_plan_refuses_legacy_file_created_after_plan(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    TokenStore._harden_path(quarantine, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        purge_quarantine=True,
    )
    write_legacy(legacy_path, "appeared-late")

    with pytest.raises(TokenStoreError, match="Legacy token existence changed"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert legacy_path.exists()
    assert quarantine.exists()


def test_no_change_plan_still_refuses_legacy_file_created_after_plan(
    monkeypatch,
    tmp_path: Path,
):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
    )
    assert plan.has_changes is False
    write_legacy(legacy_path, "appeared-late")

    with pytest.raises(TokenStoreError, match="Legacy token existence changed"):
        apply_migration_plan(plan, lambda _prompt: pytest.fail("no-op must not prompt"))

    assert legacy_path.exists()


def test_final_inventory_refuses_legacy_created_during_dotenv_cleanup(
    monkeypatch,
    tmp_path: Path,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
    )
    real_replace = migration_module._atomic_replace_protected_file

    def replace_then_create_legacy(*args, **kwargs) -> None:
        real_replace(*args, **kwargs)
        write_legacy(legacy_path, "appeared-during-apply")

    monkeypatch.setattr(
        migration_module,
        "_atomic_replace_protected_file",
        replace_then_create_legacy,
    )

    with pytest.raises(TokenStoreError, match="Legacy token remains after migration"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert default_env.read_text(encoding="utf-8") == ("GARMIN_PASSWORD=secret\nKEEP=value\n")
    assert legacy_path.exists()


@pytest.mark.parametrize("late_state", ("safe", "unsafe"))
def test_plan_refuses_dotenv_created_after_plan(
    monkeypatch,
    tmp_path: Path,
    late_state: str,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    TokenStore._harden_path(quarantine, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
        purge_quarantine=True,
    )
    if late_state == "safe":
        default_env.write_text("GARMIN_PASSWORD=appeared-late\n", encoding="utf-8")
    else:
        default_env.mkdir()

    with pytest.raises(TokenStoreError, match="Dotenv inventory"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert quarantine.exists()


def test_plan_refuses_unchanged_key_state_with_changed_dotenv_bytes(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("KEEP=before\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    quarantine = TokenStore.quarantine_path_for(store.directory)
    write_legacy(quarantine)
    TokenStore._harden_path(quarantine, directory=False)
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "missing-legacy",
        purge_quarantine=True,
    )
    default_env.write_text("KEEP=after\n", encoding="utf-8")

    with pytest.raises(TokenStoreError, match="Dotenv inventory changed"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert quarantine.exists()


def test_migration_refuses_crash_temp_created_after_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    stale = store.directory / ".token-crash.tmp"
    stale.write_text(token_payload("stale-copy"), encoding="utf-8")
    TokenStore._harden_path(stale, directory=False)

    with pytest.raises(TokenStoreError, match="appeared after plan creation"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert stale.exists()
    assert dotenv_values(default_env)["GARMIN_PASSWORD"] == "secret"


def test_migration_refuses_dotenv_recovery_created_after_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    original = b"GARMIN_PASSWORD=secret\nKEEP=value\n"
    default_env.write_bytes(original)
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    recovery_path = migration_module._dotenv_recovery_path(default_env)
    recovery_path.write_bytes(b"KEEP=value\n")
    migration_module._apply_protection(
        recovery_path,
        migration_module._capture_protection(default_env),
    )

    with pytest.raises(TokenStoreError, match="recovery state changed after plan creation"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert default_env.read_bytes() == original
    assert recovery_path.read_bytes() == b"KEEP=value\n"


def test_migration_cleans_case_insensitive_legacy_settings(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "mixed-case-legacy-token"
    write_legacy(legacy_path)
    default_env.write_text(
        "GarMin_Email=user@example.com\n"
        "garmin_password=secret\n"
        f"garmintokens_base64={legacy_path}\n"
        "KEEP=value\n",
        encoding="utf-8",
    )

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        allow_custom_legacy=True,
    )

    assert audit.stored_credentials == (default_env,)
    assert audit.legacy_exists is True
    assert plan.dotenv_cleanups[0].keys == (
        "GarMin_Email",
        "garmin_password",
        "garmintokens_base64",
    )
    assert plan.legacy_path == legacy_path

    result = apply_migration_plan(plan, lambda _prompt: True)

    assert result.quarantined_legacy_token is True
    assert dotenv_values(default_env) == {"KEEP": "value"}
    assert not legacy_path.exists()


def test_migration_removes_shadowed_duplicate_secret_binding(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text(
        "GARMIN_PASSWORD=still-on-disk\nGARMIN_PASSWORD=\nKEEP=value\n",
        encoding="utf-8",
    )
    store = write_token(tmp_path / "tokens")

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.stored_credentials == (default_env,)
    assert plan.dotenv_cleanups[0].keys == ("GARMIN_PASSWORD",)
    assert b"still-on-disk" not in plan.dotenv_cleanups[0].replacement
    assert "still-on-disk" not in " ".join(audit.issues)

    apply_migration_plan(plan, lambda _prompt: True)

    assert dotenv_values(default_env) == {"KEEP": "value"}


def test_empty_case_variant_shadows_earlier_legacy_token_path(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text(
        f"GARMINTOKENS_BASE64={tmp_path / 'shadowed-token'}\ngarmintokens_base64=\n",
        encoding="utf-8",
    )

    assert auth_module.get_legacy_token_path() == auth_module.DEFAULT_LEGACY_TOKEN_FILE


def test_empty_launcher_value_shadows_dotenv_legacy_token_path(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text(
        f"GARMINTOKENS_BASE64={tmp_path / 'shadowed-token'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("garmintokens_base64", "")

    assert auth_module.get_legacy_token_path() == auth_module.DEFAULT_LEGACY_TOKEN_FILE


def test_empty_launcher_value_shadows_dotenv_token_store(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "shadowed-store")
    default_env.write_text(f"GARMINTOKENS={store.directory}\n", encoding="utf-8")
    monkeypatch.setenv("garmintokens", "")

    plan = build_migration_plan(legacy_path=tmp_path / "no-legacy-file")

    assert plan.canonical_token == tmp_path / "garmin_tokens.json"
    assert plan.canonical_fingerprint is None
    assert plan.canonical_token.parent != store.directory


def test_bare_token_store_setting_uses_default_instead_of_cwd(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMINTOKENS\n", encoding="utf-8")

    plan = build_migration_plan(legacy_path=tmp_path / "no-legacy-file")

    assert plan.canonical_token == auth_module.DEFAULT_TOKEN_STORE / "garmin_tokens.json"


def test_higher_precedence_bare_token_store_shadows_lower_dotenv(
    monkeypatch,
    tmp_path: Path,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    shadowed_store = write_token(tmp_path / "shadowed-store")
    default_env.write_text(f"GARMINTOKENS={shadowed_store.directory}\n", encoding="utf-8")
    (tmp_path / ".env").write_text("garmintokens\n", encoding="utf-8")

    plan = build_migration_plan(legacy_path=tmp_path / "no-legacy-file")

    assert plan.canonical_token == auth_module.DEFAULT_TOKEN_STORE / "garmin_tokens.json"
    assert plan.canonical_token.parent != shadowed_store.directory


@pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL contract")
def test_migration_blocks_dotenv_with_posix_acl(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    undefined_id = 0xFFFFFFFF
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permissions, identifier)
        for tag, permissions, identifier in (
            (0x01, 0b110, undefined_id),  # owner: rw-
            (0x02, 0b100, 65534),  # named user: r--
            (0x04, 0, undefined_id),  # owning group: ---
            (0x10, 0b100, undefined_id),  # mask: r--
            (0x20, 0, undefined_id),  # other: ---
        )
    )
    setxattr = getattr(os, "setxattr", None)
    if setxattr is None:
        pytest.skip("POSIX ACL xattrs unavailable")
    try:
        setxattr(default_env, "system.posix_acl_access", acl, follow_symlinks=False)
    except OSError as exc:
        pytest.skip(f"POSIX ACL xattrs unavailable: {exc}")

    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert any("POSIX ACLs or extended attributes" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="POSIX ACLs or extended attributes"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert dotenv_values(default_env)["GARMIN_PASSWORD"] == "secret"


def test_migration_refuses_incomplete_canonical_token(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\n", encoding="utf-8")
    token_directory = tmp_path / "tokens"
    token_directory.mkdir()
    (token_directory / "garmin_tokens.json").write_text(
        json.dumps({"di_refresh_token": "refresh-only"}),
        encoding="utf-8",
    )
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)

    with pytest.raises(TokenStoreError, match="usable canonical token"):
        migrate_auth_state(
            lambda _prompt: True,
            GarminConfig(garmintokens=str(token_directory)),
            legacy_path,
            allow_custom_legacy=True,
        )

    assert legacy_path.exists()
    assert dotenv_values(default_env)["GARMIN_PASSWORD"] == "secret"


def test_migration_refuses_arbitrary_non_token_legacy_target(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    important = tmp_path / "important.txt"
    important.write_text("not a Garmin token", encoding="utf-8")

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        important,
        allow_custom_legacy=True,
    )

    assert any("unrecognized legacy token" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="unrecognized legacy token"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert important.read_text(encoding="utf-8") == "not a Garmin token"


def test_migration_refuses_hardlinked_legacy_token(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)
    external_copy = tmp_path / "external-legacy-link"
    try:
        os.link(legacy_path, external_copy)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )

    assert any("single-link regular non-redirect" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="single-link regular non-redirect"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert legacy_path.exists()
    assert external_copy.exists()


def test_custom_legacy_target_requires_explicit_scope(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    custom_legacy = tmp_path / "custom-token"
    write_legacy(custom_legacy)

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        custom_legacy,
    )

    assert any("--allow-custom-legacy" in blocker for blocker in plan.blockers)
    assert custom_legacy.exists()


def test_local_dotenv_requires_explicit_scope(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("KEEP=home\n", encoding="utf-8")
    local_env = tmp_path / ".env"
    local_env.write_text("GARMIN_PASSWORD=project-secret\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")

    blocked = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    allowed = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        include_local_env=True,
    )

    assert any("--include-local-env" in blocker for blocker in blocked.blockers)
    assert allowed.dotenv_cleanups[0].path == local_env


def test_plan_rejects_legacy_path_aliasing_canonical(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        store.token_file,
        allow_custom_legacy=True,
    )

    assert any("reserved canonical token" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="reserved canonical token"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert store.exists()


@pytest.mark.parametrize(
    "reserved_kind",
    ("migration-lock", "store-lock", "quarantine"),
)
def test_plan_rejects_legacy_alias_with_coordination_path(
    monkeypatch,
    tmp_path: Path,
    reserved_kind: str,
):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    if reserved_kind == "migration-lock":
        legacy_path = migration_module._absolute(migration_module.DEFAULT_MIGRATION_LOCK_FILE)
        write_legacy(legacy_path)
        expected = "reserved global migration lock"
    elif reserved_kind == "store-lock":
        legacy_path = store.directory / migration_module.LOCK_FILENAME
        expected = "reserved token-store lock"
    else:
        legacy_path = TokenStore.quarantine_path_for(store.directory)
        write_legacy(legacy_path)
        expected = "reserved legacy quarantine"

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )

    assert any(expected in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="aliases reserved"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert legacy_path.exists()


def test_audit_reports_legacy_launcher_variables(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    monkeypatch.setenv("GARMIN_PASSWORD", "launcher-secret")

    audit = audit_auth_state(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "no-legacy-file",
    )

    assert audit.legacy_environment_variables == ("GARMIN_PASSWORD",)
    assert audit.needs_migration is True
    assert "launcher-secret" not in " ".join(audit.issues)


def test_audit_reports_mixed_case_legacy_launcher_variable(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    monkeypatch.setenv("garmin_password", "launcher-secret")

    audit = audit_auth_state(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "no-legacy-file",
    )

    assert tuple(key.casefold() for key in audit.legacy_environment_variables) == (
        "garmin_password",
    )
    assert "launcher-secret" not in " ".join(audit.issues)


def test_migration_rolls_back_dotenv_changes_on_failure(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    original = b"GARMIN_EMAIL=user@example.com\nGARMIN_PASSWORD=secret\nKEEP=value\n"
    default_env.write_bytes(original)
    store = write_token(tmp_path / "tokens")
    legacy_path = tmp_path / "legacy-token"
    write_legacy(legacy_path)
    original_acl = None
    if os.name == "nt":
        from garmin_connect_mcp.windows_acl import read_acl_descriptor

        original_acl = read_acl_descriptor(default_env)

    def fail_after_dotenv_cleanup(_path, expected_fingerprint):
        assert expected_fingerprint
        raise OSError("simulated quarantine failure")

    monkeypatch.setattr(
        migration_module.TokenStore,
        "harden_external_artifact",
        fail_after_dotenv_cleanup,
    )

    with pytest.raises(TokenStoreError, match="migration failed"):
        migrate_auth_state(
            lambda _prompt: True,
            GarminConfig(garmintokens=str(store.directory)),
            legacy_path,
            allow_custom_legacy=True,
        )

    assert default_env.read_bytes() == original
    assert legacy_path.exists()
    if os.name == "nt":
        from garmin_connect_mcp.windows_acl import read_acl_descriptor

        assert read_acl_descriptor(default_env) == original_acl


def test_confirmed_migration_repairs_dedicated_store_permissions(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(store.directory), "/grant", "*S-1-1-0:(R)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("icacls could not create the insecure ACL test fixture")
    else:
        store.directory.chmod(0o755)

    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert plan.repair_permissions is True
    apply_migration_plan(plan, lambda _prompt: True)
    assert store.permissions_secure() is True


def test_plan_blocks_permission_repair_when_owner_preflight_fails(
    monkeypatch,
    tmp_path: Path,
):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    monkeypatch.setattr(TokenStore, "permissions_secure", lambda _self: False)

    def reject_foreign_owner(_self) -> None:
        raise TokenStoreError("token belongs to another user")

    monkeypatch.setattr(TokenStore, "validate_permission_repair", reject_foreign_owner)

    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert plan.repair_permissions is False
    assert any("cannot be repaired safely" in blocker for blocker in plan.blockers)
    assert any("another user" in blocker for blocker in plan.blockers)


def test_apply_revalidates_canonical_generation_after_permission_repair(
    monkeypatch,
    tmp_path: Path,
):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    if os.name == "nt":
        security_checks = 0

        def require_one_repair(_store: TokenStore) -> bool:
            nonlocal security_checks
            security_checks += 1
            return security_checks > 1

        monkeypatch.setattr(
            TokenStore,
            "permissions_secure",
            require_one_repair,
        )
    else:
        store.directory.chmod(0o755)
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    real_enforce_permissions = TokenStore.enforce_permissions

    def mutate_during_repair(target: TokenStore) -> None:
        real_enforce_permissions(target)
        target.token_file.write_text(token_payload("changed"), encoding="utf-8")

    monkeypatch.setattr(TokenStore, "enforce_permissions", mutate_during_repair)

    with pytest.raises(TokenStoreError, match="changed during permission repair"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert TokenStore.read_external_artifact(store.token_file).fingerprint == (
        TokenStore.validate_payload(token_payload("changed")).fingerprint
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX repair-integrity contract")
def test_plan_blocks_broadly_writable_store_repair(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "tokens")
    store.directory.chmod(0o777)

    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert plan.repair_permissions is False
    assert any("Broadly writable token-store" in blocker for blocker in plan.blockers)


def test_dotenv_temp_is_protected_while_still_empty(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    real_apply_protection = migration_module._apply_protection
    protected_temp_sizes: list[int] = []
    expected_temporary_path = migration_module._dotenv_recovery_path(default_env)

    def record_protection(path: Path, protection: FileProtection) -> None:
        if path == expected_temporary_path:
            protected_temp_sizes.append(path.stat().st_size)
        real_apply_protection(path, protection)

    monkeypatch.setattr(migration_module, "_apply_protection", record_protection)

    apply_migration_plan(plan, lambda _prompt: True)

    assert protected_temp_sizes == [0]
    assert dotenv_values(default_env) == {"KEEP": "value"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX owning-group preservation contract")
def test_cleanup_preserves_mode_and_non_parent_owning_group(monkeypatch, tmp_path: Path):
    if os.geteuid() != 0:  # pyright: ignore[reportAttributeAccessIssue]
        pytest.skip("Changing a dotenv to a non-process group requires root")
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    parent_group = default_env.parent.stat().st_gid
    different_group = parent_group + 1
    os.chown(  # pyright: ignore[reportAttributeAccessIssue]
        default_env,
        -1,
        different_group,
    )
    default_env.chmod(0o640)
    store = write_token(tmp_path / "tokens")

    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert plan.dotenv_cleanups[0].protection.group_id == different_group
    apply_migration_plan(plan, lambda _prompt: True)
    metadata = default_env.stat()
    assert metadata.st_gid == different_group
    assert stat.S_IMODE(metadata.st_mode) == 0o640
    assert dotenv_values(default_env) == {"KEEP": "value"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-integrity contract")
def test_shared_writable_dotenv_parent_blocks_before_adversarial_hook(
    monkeypatch,
    tmp_path: Path,
):
    shared = tmp_path / "shared"
    shared.mkdir()
    default_env = shared / ".garminconnect.env"
    original = b"GARMIN_PASSWORD=secret\nKEEP=value\n"
    default_env.write_bytes(original)
    protection = migration_module._capture_protection(default_env)
    victim = tmp_path / "victim"
    victim.write_text("do not touch", encoding="utf-8")
    victim.chmod(0o644)
    shared.chmod(0o777)
    protection_hooks: list[Path] = []
    monkeypatch.setattr(
        migration_module,
        "_apply_protection",
        lambda path, _protection: protection_hooks.append(path),
    )

    with pytest.raises(TokenStoreError, match="writable without sticky protection"):
        migration_module._atomic_replace_protected_file(
            default_env,
            b"KEEP=value\n",
            protection,
            expected_fingerprint=migration_module._fingerprint(original),
        )

    assert protection_hooks == []
    assert default_env.read_bytes() == original
    assert victim.read_text(encoding="utf-8") == "do not touch"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert not migration_module._dotenv_recovery_path(default_env).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX parent-integrity contract")
def test_custom_legacy_in_shared_writable_parent_is_blocked(monkeypatch, tmp_path: Path):
    configure_env_files(monkeypatch, tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    legacy_path = shared / "legacy-token"
    write_legacy(legacy_path)
    shared.chmod(0o777)
    store = write_token(tmp_path / "tokens")

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        legacy_path,
        allow_custom_legacy=True,
    )

    assert any("writable without sticky protection" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="blocked"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert legacy_path.exists()


@pytest.mark.parametrize(
    ("target_content", "recovery_content", "planned_cleanups"),
    (
        (
            b"GARMIN_PASSWORD=remove-me\nOTHER_SECRET=retain-me\n",
            b"OTHER_SECRET=retain-me\n",
            1,
        ),
        (
            b"OTHER_SECRET=retain-me\n",
            b"GARMIN_PASSWORD=remove-me\nOTHER_SECRET=retain-me\n",
            0,
        ),
    ),
    ids=("cleanup-generation", "rollback-generation-with-credentials"),
)
def test_spawn_crash_dotenv_generation_is_audited_and_confirmed_before_removal(
    monkeypatch,
    tmp_path: Path,
    target_content: bytes,
    recovery_content: bytes,
    planned_cleanups: int,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_bytes(target_content)
    store = write_token(tmp_path / "tokens")
    protection = migration_module._capture_protection(default_env)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=crash_during_dotenv_temp_write,
        args=(
            str(default_env),
            recovery_content,
            protection,
            migration_module._fingerprint(target_content),
        ),
    )

    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Crash-window subprocess timed out")

    recovery_path = migration_module._dotenv_recovery_path(default_env)
    assert process.exitcode == 73
    assert recovery_path.read_bytes() == recovery_content

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.interrupted_dotenv_writes == (recovery_path,)
    assert any("confirmed recovery" in issue for issue in audit.issues)
    assert len(plan.dotenv_cleanups) == planned_cleanups
    assert tuple(item.temporary_path for item in plan.dotenv_recoveries) == (recovery_path,)

    result = apply_migration_plan(plan, lambda _prompt: True)

    assert result.reconciled_dotenv_writes == (recovery_path,)
    assert not recovery_path.exists()
    assert default_env.read_bytes() == b"OTHER_SECRET=retain-me\n"


def test_unrecognized_deterministic_dotenv_recovery_is_never_removed(
    monkeypatch,
    tmp_path: Path,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=remove-me\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    recovery_path = migration_module._dotenv_recovery_path(default_env)
    recovery_path.write_text("UNRELATED=value\n", encoding="utf-8")
    migration_module._apply_protection(
        recovery_path,
        migration_module._capture_protection(default_env),
    )

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert recovery_path in audit.unsafe_env_files
    assert plan.dotenv_recoveries == ()
    assert any(str(recovery_path) in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="blocked"):
        apply_migration_plan(plan, lambda _prompt: True)
    assert recovery_path.read_text(encoding="utf-8") == "UNRELATED=value\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows pre-write ACL contract")
def test_crashed_dotenv_temp_never_contains_secrets_under_inherited_acl(
    monkeypatch,
    tmp_path: Path,
):
    from garmin_connect_mcp.windows_acl import (
        acl_is_owner_only,
        enforce_owner_only_acl,
        read_acl_descriptor,
    )

    default_env = configure_env_files(monkeypatch, tmp_path)
    result = subprocess.run(
        ["icacls", str(tmp_path), "/grant", "*S-1-1-0:(OI)(CI)(R)"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or acl_is_owner_only(tmp_path):
        pytest.skip("Could not create a broadly inherited parent ACL fixture")
    original = b"GARMIN_PASSWORD=remove-me\nOTHER_SECRET=retain-me\n"
    replacement = b"OTHER_SECRET=retain-me\n"
    default_env.write_bytes(original)
    enforce_owner_only_acl(default_env, directory=False)
    protection = migration_module._capture_protection(default_env)
    expected_acl = read_acl_descriptor(default_env)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=crash_during_dotenv_temp_write,
        args=(
            str(default_env),
            replacement,
            protection,
            migration_module._fingerprint(original),
        ),
    )

    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("Crash-window subprocess timed out")

    temporary_path = migration_module._dotenv_recovery_path(default_env)
    assert process.exitcode == 73
    assert temporary_path.read_bytes() == replacement
    assert read_acl_descriptor(temporary_path) == expected_acl


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL preservation contract")
def test_cleanup_preserves_exact_protected_windows_acl(monkeypatch, tmp_path: Path):
    from garmin_connect_mcp.windows_acl import (
        enforce_owner_only_acl,
        read_acl_descriptor,
    )

    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    enforce_owner_only_acl(default_env, directory=False)
    original_acl = read_acl_descriptor(default_env)
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    apply_migration_plan(plan, lambda _prompt: True)

    assert dotenv_values(default_env) == {"KEEP": "value"}
    assert read_acl_descriptor(default_env) == original_acl


def test_two_threads_cannot_reapply_one_migration_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    barrier = threading.Barrier(2)

    def apply_once() -> str:
        barrier.wait(timeout=10)
        try:
            apply_migration_plan(plan, lambda _prompt: True)
        except TokenStoreError:
            return "stale"
        return "applied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(lambda _index: apply_once(), range(2)))

    assert outcomes == ["applied", "stale"]
    assert dotenv_values(default_env) == {"KEEP": "value"}


def test_two_processes_cannot_restore_credentials_from_stale_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=apply_plan_worker, args=(plan, ready, start, results))
        for _index in range(2)
    ]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["ready", "ready"]
        start.set()
        outcomes = sorted([results.get(timeout=10), results.get(timeout=10)])
    finally:
        start.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert outcomes == ["applied", "stale"]
    assert all(process.exitcode == 0 for process in processes)
    assert dotenv_values(default_env) == {"KEEP": "value"}


def test_different_stores_share_thread_migration_lock(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    stores = [write_token(tmp_path / f"tokens-{index}", str(index)) for index in range(2)]
    plans = [
        build_migration_plan(GarminConfig(garmintokens=str(store.directory))) for store in stores
    ]
    barrier = threading.Barrier(2)

    def apply_once(plan: MigrationPlan) -> str:
        barrier.wait(timeout=10)
        try:
            apply_migration_plan(plan, lambda _prompt: True)
        except TokenStoreError:
            return "stale"
        return "applied"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(apply_once, plans))

    assert outcomes == ["applied", "stale"]
    assert dotenv_values(default_env) == {"KEEP": "value"}


def test_different_stores_share_process_migration_lock(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\nKEEP=value\n", encoding="utf-8")
    stores = [write_token(tmp_path / f"tokens-{index}", str(index)) for index in range(2)]
    plans = [
        build_migration_plan(GarminConfig(garmintokens=str(store.directory))) for store in stores
    ]
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=apply_plan_worker, args=(plan, ready, start, results))
        for plan in plans
    ]
    for process in processes:
        process.start()
    try:
        assert [ready.get(timeout=10), ready.get(timeout=10)] == ["ready", "ready"]
        start.set()
        outcomes = sorted([results.get(timeout=10), results.get(timeout=10)])
    finally:
        start.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert outcomes == ["applied", "stale"]
    assert all(process.exitcode == 0 for process in processes)
    assert dotenv_values(default_env) == {"KEEP": "value"}


def test_migration_refuses_unsafe_dotenv_path(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.mkdir()
    store = write_token(tmp_path / "tokens")

    plan = build_migration_plan(
        GarminConfig(garmintokens=str(store.directory)),
        tmp_path / "no-legacy-file",
    )

    assert any("unsafe, unreadable" in blocker for blocker in plan.blockers)
    with pytest.raises(TokenStoreError, match="unsafe, unreadable"):
        apply_migration_plan(plan, lambda _prompt: True)


def test_migration_refuses_hardlinked_dotenv(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\n", encoding="utf-8")
    external_copy = tmp_path / "external-dotenv-link"
    try:
        os.link(default_env, external_copy)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")
    store = write_token(tmp_path / "tokens")

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.unsafe_env_files == (default_env,)
    assert any("unsafe, unreadable" in blocker for blocker in plan.blockers)
    assert external_copy.read_text(encoding="utf-8") == "GARMIN_PASSWORD=secret\n"


def test_apply_refuses_dotenv_redirect_created_after_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    unrelated = tmp_path / "unrelated.env"
    unrelated.write_text("KEEP=untouched\n", encoding="utf-8")
    default_env.unlink()
    try:
        default_env.symlink_to(unrelated)
    except OSError as exc:
        pytest.skip(f"Symbolic links unavailable: {exc}")

    with pytest.raises(TokenStoreError, match="single-link regular non-redirect"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert unrelated.read_text(encoding="utf-8") == "KEEP=untouched\n"


def test_apply_refuses_oversized_dotenv_created_after_plan(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))
    oversized = b"x" * (migration_module.MAX_DOTENV_BYTES + 1)
    default_env.write_bytes(oversized)

    with pytest.raises(TokenStoreError, match="inventory became unsafe"):
        apply_migration_plan(plan, lambda _prompt: True)

    assert default_env.stat().st_size == len(oversized)


def test_invalid_utf8_dotenv_becomes_audit_issue_and_plan_blocker(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_bytes(b"GARMIN_PASSWORD=secret\xff\n")
    store = write_token(tmp_path / "tokens")

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.unsafe_env_files == (default_env,)
    assert any("not valid UTF-8" in issue for issue in audit.issues)
    assert any("non-UTF-8 dotenv" in blocker for blocker in plan.blockers)
    assert "secret" not in " ".join((*audit.issues, *plan.blockers))


def test_dotenv_read_failure_becomes_audit_issue_and_plan_blocker(
    monkeypatch,
    tmp_path: Path,
):
    default_env = configure_env_files(monkeypatch, tmp_path)
    default_env.write_text("GARMIN_PASSWORD=secret\n", encoding="utf-8")
    store = write_token(tmp_path / "tokens")
    original_reader = migration_module._read_dotenv_snapshot

    def fail_selected_read(path: Path):
        if path == default_env:
            raise PermissionError("simulated read denial")
        return original_reader(path)

    monkeypatch.setattr(migration_module, "_read_dotenv_snapshot", fail_selected_read)

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    plan = build_migration_plan(GarminConfig(garmintokens=str(store.directory)))

    assert audit.unsafe_env_files == (default_env,)
    assert any("unreadable" in issue for issue in audit.issues)
    assert any("unreadable" in blocker for blocker in plan.blockers)


def test_audit_resolves_custom_token_store_only_for_legacy_migration(monkeypatch, tmp_path: Path):
    default_env = configure_env_files(monkeypatch, tmp_path)
    store = write_token(tmp_path / "custom-tokens")
    default_env.write_text(f"GarMinTokens={store.directory}\n", encoding="utf-8")

    audit = audit_auth_state(legacy_path=tmp_path / "no-legacy-file")

    assert audit.token_exists is True
    assert audit.legacy_runtime_config_files == (default_env,)
    assert audit.needs_migration is True
