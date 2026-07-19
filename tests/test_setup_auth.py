"""Tests for the interactive authentication command."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

import garmin_connect_mcp.auth as auth_module
import garmin_connect_mcp.auth_migration as migration_module
from garmin_connect_mcp.auth_migration import (
    DotenvCleanupPlan,
    DotenvRecoveryPlan,
    FileProtection,
    MigrationPlan,
    MigrationResult,
)
from garmin_connect_mcp.scripts import setup_auth
from garmin_connect_mcp.token_store import TokenStore, TokenStoreAudit


class FakeSessionManager:
    def __init__(self, store: TokenStore):
        self.store = store
        self.calls: list[tuple[str, str, str]] = []

    def authenticate(self, email: str, password: str, prompt_mfa: Any) -> TokenStore:
        self.calls.append((email, password, prompt_mfa()))
        return self.store


def clean_audit(*_args, **_kwargs) -> TokenStoreAudit:
    return TokenStoreAudit(True, True, False, False, (), (), (), (), ())


def test_interactive_auth_keeps_password_in_prompt_flow(monkeypatch, tmp_path: Path, capsys):
    manager = FakeSessionManager(TokenStore(tmp_path / "tokens"))
    secrets = iter(["secret-password", "123456"])
    monkeypatch.setattr(setup_auth, "audit_auth_state", clean_audit)

    result = setup_auth.authenticate_interactively(
        manager=manager,  # type: ignore[arg-type]
        input_fn=lambda _prompt: "user@example.com",
        secret_input_fn=lambda _prompt: next(secrets),
    )

    output = capsys.readouterr().out
    assert result == 0
    assert manager.calls == [("user@example.com", "secret-password", "123456")]
    assert "secret-password" not in output
    assert "123456" not in output
    assert "not saved" in output


def test_interactive_auth_rejects_empty_credentials(tmp_path: Path):
    manager = FakeSessionManager(TokenStore(tmp_path / "tokens"))

    result = setup_auth.authenticate_interactively(
        manager=manager,  # type: ignore[arg-type]
        input_fn=lambda _prompt: "",
        secret_input_fn=lambda _prompt: "",
    )

    assert result == 1
    assert manager.calls == []


def test_interactive_auth_does_not_normalize_password_whitespace(monkeypatch, tmp_path: Path):
    manager = FakeSessionManager(TokenStore(tmp_path / "tokens"))
    secrets = iter(["  exact password  ", "123456"])
    monkeypatch.setattr(setup_auth, "audit_auth_state", clean_audit)

    result = setup_auth.authenticate_interactively(
        manager=manager,  # type: ignore[arg-type]
        input_fn=lambda _prompt: "user@example.com",
        secret_input_fn=lambda _prompt: next(secrets),
    )

    assert result == 0
    assert manager.calls[0][1] == "  exact password  "


def test_migration_plan_displays_exact_targets_and_key_names(capsys, tmp_path: Path):
    canonical = tmp_path / "tokens" / "garmin_tokens.json"
    dotenv = tmp_path / ".garminconnect.env"
    recovery = tmp_path / ".garmin-connect-mcp-auth-recovery.tmp"
    legacy = tmp_path / "legacy-token"
    quarantine = TokenStore.quarantine_path_for(canonical.parent)
    protection = FileProtection(0o600, None, None)
    plan = MigrationPlan(
        migration_lock=tmp_path / ".auth-migration.lock",
        canonical_token=canonical,
        canonical_fingerprint="canonical-fingerprint",
        dotenv_cleanups=(
            DotenvCleanupPlan(
                dotenv,
                ("GARMIN_EMAIL", "GARMIN_PASSWORD"),
                "dotenv-fp",
                b"KEEP=value\n",
                protection,
            ),
        ),
        repair_permissions=True,
        legacy_path=legacy,
        legacy_fingerprint="legacy-fingerprint",
        quarantine_path=quarantine,
        quarantine_fingerprint=None,
        purge_quarantine=False,
        blockers=(),
        dotenv_recoveries=(
            DotenvRecoveryPlan(
                dotenv,
                recovery,
                "dotenv-fp",
                "recovery-fp",
                protection,
            ),
        ),
        possible_disclosure_paths=(legacy,),
    )

    setup_auth._print_migration_plan(plan)

    output = capsys.readouterr().out
    assert str(canonical) in output
    assert str(plan.migration_lock) in output
    assert str(dotenv) in output
    assert str(recovery) in output
    assert str(legacy) in output
    assert str(quarantine) in output
    assert "GARMIN_EMAIL, GARMIN_PASSWORD" in output
    assert "dotenv-fp" not in output
    assert "recovery-fp" not in output
    assert "may already have exposed" in output
    assert "Re-authenticate and rotate" in output


def test_migrate_preserves_disclosure_warning_after_local_repair(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    canonical = tmp_path / "tokens" / "garmin_tokens.json"
    exposed = tmp_path / "legacy-token"
    quarantine = canonical.parent / ".legacy-token-quarantine.json"
    plan = MigrationPlan(
        migration_lock=tmp_path / ".migration.lock",
        canonical_token=canonical,
        canonical_fingerprint="canonical",
        dotenv_cleanups=(),
        repair_permissions=False,
        legacy_path=exposed,
        legacy_fingerprint="do-not-print-token-secret",
        quarantine_path=quarantine,
        quarantine_fingerprint=None,
        purge_quarantine=False,
        blockers=(),
        possible_disclosure_paths=(exposed,),
    )
    result = MigrationResult(
        changed=True,
        quarantined_legacy_token=True,
        quarantine_path=quarantine,
        purged_quarantine=False,
        cleaned_env_files=(),
        repaired_permissions=False,
        possible_disclosure_paths=(exposed,),
    )
    remaining = TokenStoreAudit(
        token_exists=True,
        token_permissions_secure=True,
        legacy_exists=False,
        quarantine_exists=True,
        stored_credentials=(),
        unsafe_env_files=(),
        legacy_runtime_config_files=(),
        legacy_environment_variables=(),
        issues=("recoverable quarantine",),
    )
    monkeypatch.setattr(setup_auth, "build_migration_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(setup_auth, "apply_migration_plan", lambda *_args: result)
    monkeypatch.setattr(setup_auth, "audit_auth_state", lambda: remaining)

    assert setup_auth.migrate(assume_yes=True) == 0

    output = capsys.readouterr().out
    assert output.count("may already have exposed") == 2
    assert output.count("Re-authenticate and rotate") == 2
    assert str(exposed) in output
    assert "do-not-print-token-secret" not in output


def test_doctor_and_migrate_refuse_invalid_utf8_without_traceback(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    default_env = tmp_path / ".garminconnect.env"
    default_env.write_bytes(b"GARMIN_PASSWORD=leaked-value-123\xff\n")
    monkeypatch.setattr(auth_module, "DEFAULT_ENV_FILE", default_env)
    monkeypatch.setattr(
        migration_module,
        "DEFAULT_MIGRATION_LOCK_FILE",
        tmp_path / ".auth-migration.lock",
    )
    monkeypatch.chdir(tmp_path)

    assert setup_auth.doctor() == 1
    assert setup_auth.migrate(assume_yes=True) == 1

    output = capsys.readouterr().out
    assert "unsafe" in output.lower() or "unreadable" in output.lower()
    assert "leaked-value-123" not in output


def test_doctor_preserves_and_reports_owned_token_crash_temp(monkeypatch, capsys, tmp_path: Path):
    default_env = tmp_path / ".garminconnect.env"
    monkeypatch.setattr(auth_module, "DEFAULT_ENV_FILE", default_env)
    monkeypatch.setattr(
        migration_module,
        "DEFAULT_MIGRATION_LOCK_FILE",
        tmp_path / ".auth-migration.lock",
    )
    monkeypatch.chdir(tmp_path)
    for key in ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS_BASE64"):
        monkeypatch.delenv(key, raising=False)

    store = TokenStore(tmp_path / "tokens")
    store.replace_payload(
        json.dumps(
            {
                "di_token": "access",
                "di_refresh_token": "refresh",
                "di_client_id": "client",
            }
        )
    )
    monkeypatch.setenv("GARMINTOKENS", str(store.directory))
    stale = store.directory / ".token-crash.tmp"
    stale.write_text("crash remnant", encoding="utf-8")
    TokenStore._harden_path(stale, directory=False)

    assert setup_auth.doctor() == 1

    output = capsys.readouterr().out
    assert "Canonical token: present" in output
    assert "Permissions: owner-only" in output
    assert "Interrupted token writes: 1" in output
    assert "preserved for recovery" in output
    assert stale.exists()

    if os.name == "nt":
        result = subprocess.run(
            ["icacls", str(stale), "/grant", "*S-1-1-0:(R)"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip("Could not create an unsafe interrupted-token ACL fixture")
    else:
        stale.chmod(0o644)

    assert setup_auth.doctor() == 1
    unsafe_output = capsys.readouterr().out
    assert "Permissions: unsafe" in unsafe_output
    assert "may have exposed secrets" in unsafe_output
    assert stale.exists()


def test_doctor_reports_interrupted_dotenv_migration_without_secret_values(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    recovery = tmp_path / ".garmin-connect-mcp-auth-recovery.tmp"
    audit = TokenStoreAudit(
        token_exists=True,
        token_permissions_secure=True,
        legacy_exists=False,
        quarantine_exists=False,
        stored_credentials=(),
        unsafe_env_files=(),
        legacy_runtime_config_files=(),
        legacy_environment_variables=(),
        issues=(f"Interrupted dotenv migration: {recovery}",),
        interrupted_dotenv_writes=(recovery,),
    )
    monkeypatch.setattr(setup_auth, "audit_auth_state", lambda: audit)

    assert setup_auth.doctor() == 1

    output = capsys.readouterr().out
    assert "Interrupted dotenv migrations: 1" in output
    assert str(recovery) in output


def test_migrate_never_reports_success_with_unexpected_remaining_legacy_state(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    canonical = tmp_path / "tokens" / "garmin_tokens.json"
    plan = MigrationPlan(
        migration_lock=tmp_path / ".migration.lock",
        canonical_token=canonical,
        canonical_fingerprint="canonical",
        dotenv_cleanups=(),
        repair_permissions=True,
        legacy_path=None,
        legacy_fingerprint=None,
        quarantine_path=canonical.parent / ".legacy-token-quarantine.json",
        quarantine_fingerprint=None,
        purge_quarantine=False,
        blockers=(),
    )
    result = MigrationResult(True, False, plan.quarantine_path, False, (), True)
    remaining = TokenStoreAudit(
        token_exists=True,
        token_permissions_secure=True,
        legacy_exists=True,
        quarantine_exists=False,
        stored_credentials=(),
        unsafe_env_files=(),
        legacy_runtime_config_files=(),
        legacy_environment_variables=(),
        issues=("legacy remains",),
    )
    monkeypatch.setattr(setup_auth, "build_migration_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(setup_auth, "apply_migration_plan", lambda *_args: result)
    monkeypatch.setattr(setup_auth, "audit_auth_state", lambda: remaining)

    assert setup_auth.migrate(assume_yes=True) == 1

    output = capsys.readouterr().out
    assert "migration is incomplete" in output
    assert "Migration complete" not in output
