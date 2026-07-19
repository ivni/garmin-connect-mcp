"""Interactive authentication, audit, and migration commands."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path

from ..auth import GarminConfig
from ..auth_migration import (
    MigrationPlan,
    apply_migration_plan,
    audit_auth_state,
    build_migration_plan,
)
from ..client import GarminAPIError
from ..session import GarminSessionManager, get_session_manager
from ..token_store import TokenStoreAudit, TokenStoreError

InputFunction = Callable[[str], str]
SecretInputFunction = Callable[[str], str]


def authenticate_interactively(
    manager: GarminSessionManager | None = None,
    input_fn: InputFunction = input,
    secret_input_fn: SecretInputFunction = getpass.getpass,
) -> int:
    """Authenticate once without persisting account credentials."""
    print("Garmin Connect MCP - Authentication")
    print("Credentials are used only for this login and are not saved.")
    email = input_fn("Email: ").strip()
    password = secret_input_fn("Password: ")

    if not email or not password:
        print("Error: email and password are required.")
        return 1

    def prompt_for_mfa() -> str:
        return secret_input_fn("MFA one-time code: ").strip()

    session_manager = manager or get_session_manager()
    try:
        store = session_manager.authenticate(email, password, prompt_for_mfa)
    except GarminAPIError as exc:
        print(f"Authentication failed: {exc.message}")
        return 1

    print("Authentication successful.")
    print(f"Tokens saved to: {store.token_file}")

    audit = audit_auth_state(GarminConfig(garmintokens=str(store.directory)))
    if audit.needs_migration:
        print(
            "Legacy authentication artifacts still exist. "
            "Run 'garmin-connect-mcp auth migrate' after reviewing them."
        )
    return 0


def doctor() -> int:
    """Report authentication storage health without exposing secrets."""
    audit = audit_auth_state()
    print("Garmin Connect MCP - Authentication Doctor")
    print(f"Canonical token: {'present' if audit.token_exists else 'missing or invalid'}")
    print(f"Permissions: {'owner-only' if audit.token_permissions_secure else 'unsafe'}")
    print(f"Legacy token copy: {'present' if audit.legacy_exists else 'absent'}")
    print(f"Legacy quarantine: {'present' if audit.quarantine_exists else 'absent'}")
    if audit.legacy_exists:
        print(
            "Legacy token protection: "
            + ("owner-only" if audit.legacy_permissions_secure else "unsafe")
        )
    if audit.quarantine_exists:
        print(
            "Legacy quarantine protection: "
            + ("owner-only" if audit.quarantine_permissions_secure else "unsafe")
        )
    print(f"Interrupted token writes: {len(audit.interrupted_writes)}")
    print(f"Interrupted dotenv migrations: {len(audit.interrupted_dotenv_writes)}")
    print(f"Dotenv files with legacy auth data: {len(audit.stored_credentials)}")
    print(f"Unsafe or unreadable dotenv paths: {len(audit.unsafe_env_files)}")
    print(f"Dotenv files with runtime token path: {len(audit.legacy_runtime_config_files)}")
    if audit.legacy_environment_variables:
        print(
            "Legacy launcher environment variables: "
            + ", ".join(audit.legacy_environment_variables)
        )

    for issue in audit.issues:
        print(f"- {issue}")
    return 1 if audit.issues else 0


def migrate(
    assume_yes: bool = False,
    input_fn: InputFunction = input,
    *,
    include_local_env: bool = False,
    allow_custom_legacy: bool = False,
    purge_quarantine: bool = False,
) -> int:
    """Apply a displayed, recoverable migration plan."""

    def confirm(prompt: str) -> bool:
        if assume_yes:
            return True
        return input_fn(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}

    try:
        plan = build_migration_plan(
            include_local_env=include_local_env,
            allow_custom_legacy=allow_custom_legacy,
            purge_quarantine=purge_quarantine,
        )
        _print_migration_plan(plan)
        result = apply_migration_plan(plan, confirm)
    except TokenStoreError as exc:
        print(f"Migration refused: {exc}")
        return 1

    if not result.changed:
        print("No authentication artifacts were changed.")
        remaining = audit_auth_state()
        if remaining.legacy_environment_variables:
            print(
                "Remove legacy variables from the parent process or MCP launcher: "
                + ", ".join(remaining.legacy_environment_variables)
            )
            return 1
        if remaining.legacy_runtime_config_files:
            print("Move GARMINTOKENS from dotenv into the MCP launcher environment.")
            return 1
        if _has_unexpected_remaining_state(remaining, allow_quarantine=True):
            print("Authentication migration is incomplete; run 'auth doctor' and re-plan.")
            return 1
        if remaining.quarantine_exists:
            print("Verify authentication, then run 'garmin-connect-mcp auth migrate --purge'.")
        return 0

    if (
        result.quarantined_legacy_token
        and result.quarantine_path is not None
        and plan.legacy_path is not None
    ):
        print(f"Moved deprecated token into owner-only quarantine: {result.quarantine_path}")
        print(f"Recovery: move it back to {plan.legacy_path}")
        print("After verification, remove it with 'garmin-connect-mcp auth migrate --purge'.")
    if result.purged_quarantine and result.quarantine_path is not None:
        print(f"Permanently removed legacy-token quarantine: {result.quarantine_path}")
    for env_path in result.cleaned_env_files:
        print(f"Removed legacy Garmin auth values from: {env_path}")
    for recovery_path in result.reconciled_dotenv_writes:
        print(f"Removed confirmed interrupted dotenv generation: {recovery_path}")
    if result.repaired_permissions:
        print("Repaired canonical token-store permissions.")
    if result.repaired_quarantine_permissions:
        print("Repaired owner-only legacy-quarantine permissions.")
    if result.possible_disclosure_paths:
        _print_disclosure_warning(result.possible_disclosure_paths, completed=True)
    remaining = audit_auth_state()
    if remaining.legacy_environment_variables:
        print(
            "Remove legacy variables from the parent process or MCP launcher: "
            + ", ".join(remaining.legacy_environment_variables)
        )
        print("Local migration complete; launcher cleanup is still required.")
        return 1
    if remaining.legacy_runtime_config_files:
        print("Move GARMINTOKENS from dotenv into the MCP launcher environment.")
        print("Local migration complete; launcher configuration is still required.")
        return 1
    if _has_unexpected_remaining_state(
        remaining,
        allow_quarantine=not purge_quarantine,
    ):
        print("Authentication migration is incomplete; run 'auth doctor' and re-plan.")
        return 1
    if remaining.quarantine_exists and not purge_quarantine:
        print("Recoverable migration complete; verify authentication before optional purge.")
        return 0
    print("Migration complete. Restart running MCP server processes.")
    return 0


def _has_unexpected_remaining_state(
    audit: TokenStoreAudit,
    *,
    allow_quarantine: bool,
) -> bool:
    """Fail closed unless only an explicitly recoverable quarantine remains."""
    return bool(
        not audit.token_exists
        or not audit.token_permissions_secure
        or audit.legacy_exists
        or (audit.quarantine_exists and not allow_quarantine)
        or audit.stored_credentials
        or audit.unsafe_env_files
        or audit.legacy_runtime_config_files
        or audit.legacy_environment_variables
        or audit.interrupted_writes
        or audit.interrupted_dotenv_writes
        or not audit.legacy_permissions_secure
        or not audit.quarantine_permissions_secure
    )


def _print_migration_plan(plan: MigrationPlan) -> None:
    """Render exact paths and key names without secret values."""
    print("Migration plan (no secret values):")
    print(f"- serialize migration with: {plan.migration_lock}")
    print(f"- canonical token required: {plan.canonical_token}")
    for cleanup in plan.dotenv_cleanups:
        print(f"- remove keys {', '.join(cleanup.keys)} from: {cleanup.path}")
    for recovery in plan.dotenv_recoveries:
        print(f"- remove confirmed interrupted generation: {recovery.temporary_path}")
        print(f"  target dotenv: {recovery.target_path}")
    if plan.repair_permissions:
        print(f"- repair owner-only permissions: {plan.canonical_token.parent}")
    if plan.repair_quarantine_permissions and plan.quarantine_path is not None:
        print(f"- repair owner-only quarantine permissions: {plan.quarantine_path}")
    if plan.possible_disclosure_paths:
        _print_disclosure_warning(plan.possible_disclosure_paths, completed=False)
    if plan.legacy_path is not None and plan.quarantine_path is not None:
        print(f"- atomically quarantine: {plan.legacy_path}")
        print(f"  recovery path: {plan.quarantine_path}")
    if plan.purge_quarantine and plan.quarantine_path is not None:
        print(f"- permanently delete quarantine: {plan.quarantine_path}")
    if not plan.has_changes:
        print("- no local changes")
    for blocker in plan.blockers:
        print(f"- BLOCKED: {blocker}")


def _print_disclosure_warning(paths: tuple[Path, ...], *, completed: bool) -> None:
    """Keep possible prior disclosure visible before and after local repair."""
    phase = "Migration repaired local protection, but" if completed else "WARNING:"
    print(
        f"{phase} these token artifacts may already have exposed Garmin session secrets: "
        + ", ".join(str(path) for path in paths)
    )
    print(
        "Re-authenticate and rotate the affected Garmin session; chmod or deletion is not enough."
    )


def main(
    action: str = "login",
    assume_yes: bool = False,
    *,
    include_local_env: bool = False,
    allow_custom_legacy: bool = False,
    purge_quarantine: bool = False,
) -> int:
    """Run the selected authentication command."""
    if action == "login":
        return authenticate_interactively()
    if action == "doctor":
        return doctor()
    if action == "migrate":
        return migrate(
            assume_yes=assume_yes,
            include_local_env=include_local_env,
            allow_custom_legacy=allow_custom_legacy,
            purge_quarantine=purge_quarantine,
        )
    raise ValueError(f"Unknown authentication action: {action}")
