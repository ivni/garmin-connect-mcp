"""Audit and explicit, recoverable migration of legacy auth artifacts."""

from __future__ import annotations

import hashlib
import io
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from .auth import (
    DEFAULT_LEGACY_TOKEN_FILE,
    LOCAL_ENV_FILE,
    GarminConfig,
    _case_insensitive_lookup,
    get_env_file_candidates,
    load_config,
)
from .token_store import (
    LOCK_FILENAME,
    TokenStore,
    TokenStoreAudit,
    TokenStoreError,
    atomic_replace_file,
)
from .windows_acl import WindowsAclDescriptor

LEGACY_ENV_KEYS = ("GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMINTOKENS_BASE64")
DEFAULT_MIGRATION_LOCK_FILE = Path.home() / ".garmin-connect-mcp-auth-migration.lock"
MAX_DOTENV_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FileProtection:
    """Platform protection metadata preserved across atomic replacement."""

    mode: int
    owner_id: int | None
    windows_acl: WindowsAclDescriptor | None
    posix_xattrs: tuple[str, ...] = ()
    group_id: int | None = None


EnvSnapshot = tuple[bytes, FileProtection]


@dataclass(frozen=True)
class DotenvSnapshot:
    """One safely opened dotenv generation used for inventory and resolution."""

    path: Path
    content: bytes
    values: dict[str, str | None]
    legacy_key_spellings: tuple[str, ...]


@dataclass(frozen=True)
class DotenvCleanupPlan:
    """A fingerprinted dotenv edit that never contains secret values."""

    path: Path
    keys: tuple[str, ...]
    fingerprint: str
    replacement: bytes
    protection: FileProtection


@dataclass(frozen=True)
class DotenvInventoryEntry:
    """One positive or negative dotenv fact captured by a migration plan."""

    path: Path
    exists: bool
    fingerprint: str | None
    protection: FileProtection | None
    legacy_key_spellings: tuple[str, ...]


@dataclass(frozen=True)
class DotenvRecoveryPlan:
    """One exact, validated deterministic dotenv crash artifact."""

    target_path: Path
    temporary_path: Path
    target_fingerprint: str
    temporary_fingerprint: str
    protection: FileProtection


@dataclass(frozen=True)
class MigrationPlan:
    """Immutable destructive scope prepared before user confirmation."""

    migration_lock: Path
    canonical_token: Path
    canonical_fingerprint: str | None
    dotenv_cleanups: tuple[DotenvCleanupPlan, ...]
    repair_permissions: bool
    legacy_path: Path | None
    legacy_fingerprint: str | None
    quarantine_path: Path | None
    quarantine_fingerprint: str | None
    purge_quarantine: bool
    blockers: tuple[str, ...]
    dotenv_recoveries: tuple[DotenvRecoveryPlan, ...] = ()
    dotenv_inventory: tuple[DotenvInventoryEntry, ...] = ()
    legacy_candidate_path: Path | None = None
    legacy_expected_exists: bool = False
    quarantine_expected_exists: bool = False
    legacy_protection: FileProtection | None = None
    quarantine_protection: FileProtection | None = None
    repair_quarantine_permissions: bool = False
    possible_disclosure_paths: tuple[Path, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(
            self.dotenv_cleanups
            or self.dotenv_recoveries
            or self.repair_permissions
            or self.repair_quarantine_permissions
            or self.legacy_fingerprint
            or (self.purge_quarantine and self.quarantine_fingerprint)
        )


@dataclass(frozen=True)
class MigrationResult:
    """Summary of an applied migration plan."""

    changed: bool
    quarantined_legacy_token: bool
    quarantine_path: Path | None
    purged_quarantine: bool
    cleaned_env_files: tuple[Path, ...]
    repaired_permissions: bool
    reconciled_dotenv_writes: tuple[Path, ...] = ()
    repaired_quarantine_permissions: bool = False
    possible_disclosure_paths: tuple[Path, ...] = ()


def audit_auth_state(
    config: GarminConfig | None = None,
    legacy_path: Path | None = None,
) -> TokenStoreAudit:
    """Inspect auth artifacts without reading or displaying secret values."""
    env_candidates = tuple(_absolute(path) for path in get_env_file_candidates())
    env_paths = tuple(path for path in env_candidates if os.path.lexists(path))
    env_snapshots, unsafe_env_files = _load_dotenv_snapshots(env_paths)
    dotenv_recoveries, unsafe_recovery_paths = _load_dotenv_recoveries(
        env_candidates,
        env_snapshots,
    )
    unsafe_env_files = tuple(dict.fromkeys((*unsafe_env_files, *unsafe_recovery_paths)))
    env_values = {path: snapshot.values for path, snapshot in env_snapshots.items()}
    runtime_config = _resolve_migration_config(config, env_values)

    store = TokenStore(runtime_config.garmintokens)
    location_error: str | None = None
    interrupted_writes: tuple[Path, ...] = ()
    try:
        store.validate_dedicated_store()
        interrupted_writes = store.interrupted_writes()
    except TokenStoreError as exc:
        location_error = str(exc)
    resolved_legacy_path = _resolve_legacy_token_path(legacy_path, env_values)
    quarantine_path = TokenStore.quarantine_path_for(store.directory)
    token_exists = store.exists()
    permissions = store.permissions_secure() if token_exists else False
    legacy_exists = os.path.lexists(resolved_legacy_path)
    quarantine_exists = os.path.lexists(quarantine_path)
    legacy_permissions_secure = not legacy_exists
    quarantine_permissions_secure = not quarantine_exists
    stored_credentials = tuple(
        path for path, snapshot in env_snapshots.items() if snapshot.legacy_key_spellings
    )
    legacy_runtime_config_files = tuple(
        path
        for path, values in env_values.items()
        if _case_insensitive_lookup(values, "GARMINTOKENS")[0]
    )
    legacy_environment_variables = _matching_keys(
        os.environ,
        LEGACY_ENV_KEYS,
    )

    issues: list[str] = []
    if location_error is not None:
        issues.append(location_error)
    if not token_exists:
        issues.append(f"The canonical Garmin token store is missing or invalid: {store.token_file}")
    if token_exists and not permissions:
        issues.append(f"The canonical token store is not owner-only: {store.token_file}")
    if interrupted_writes:
        issues.append(
            "Interrupted token writes were preserved for recovery: "
            + ", ".join(str(path) for path in interrupted_writes)
        )
    if dotenv_recoveries:
        issues.append(
            "Interrupted dotenv migrations were preserved for confirmed recovery: "
            + ", ".join(str(recovery.temporary_path) for recovery in dotenv_recoveries)
        )
    if legacy_exists:
        issues.append(f"A deprecated secondary token artifact exists: {resolved_legacy_path}")
        try:
            TokenStore.read_external_artifact(resolved_legacy_path)
        except TokenStoreError:
            issues.append(
                f"The deprecated artifact is unsafe or not a recognized Garmin token: "
                f"{resolved_legacy_path}"
            )
        legacy_permissions_secure = TokenStore.external_artifact_permissions_secure(
            resolved_legacy_path
        )
        if not legacy_permissions_secure:
            issues.append(
                "The deprecated token artifact is not owner-only and may already have "
                f"exposed Garmin session secrets: {resolved_legacy_path}"
            )
    if quarantine_exists:
        issues.append(f"A recoverable legacy-token quarantine awaits purge: {quarantine_path}")
        try:
            TokenStore.read_external_artifact(quarantine_path)
        except TokenStoreError:
            issues.append(
                "The legacy-token quarantine is unsafe or not a recognized Garmin token: "
                f"{quarantine_path}"
            )
        quarantine_permissions_secure = TokenStore.external_artifact_permissions_secure(
            quarantine_path
        )
        if not quarantine_permissions_secure:
            issues.append(
                "The legacy-token quarantine is not owner-only and may already have exposed "
                f"Garmin session secrets: {quarantine_path}"
            )
    if stored_credentials:
        issues.append(
            "Legacy Garmin credentials remain in dotenv files: "
            + ", ".join(str(path) for path in stored_credentials)
        )
    if unsafe_env_files:
        issues.append(
            "Dotenv paths are unsafe, unreadable, too large, or not valid UTF-8: "
            + ", ".join(str(path) for path in unsafe_env_files)
        )
    if legacy_runtime_config_files:
        issues.append(
            "A custom token-store path remains configured only in dotenv: "
            + ", ".join(str(path) for path in legacy_runtime_config_files)
        )
    if legacy_environment_variables:
        issues.append(
            "Legacy authentication environment variables are still supplied by the launcher."
        )

    return TokenStoreAudit(
        token_exists=token_exists,
        token_permissions_secure=permissions,
        legacy_exists=legacy_exists,
        quarantine_exists=quarantine_exists,
        stored_credentials=stored_credentials,
        unsafe_env_files=unsafe_env_files,
        legacy_runtime_config_files=legacy_runtime_config_files,
        legacy_environment_variables=legacy_environment_variables,
        issues=tuple(issues),
        interrupted_writes=interrupted_writes,
        interrupted_dotenv_writes=tuple(recovery.temporary_path for recovery in dotenv_recoveries),
        legacy_permissions_secure=legacy_permissions_secure,
        quarantine_permissions_secure=quarantine_permissions_secure,
    )


def build_migration_plan(
    config: GarminConfig | None = None,
    legacy_path: Path | None = None,
    *,
    include_local_env: bool = False,
    allow_custom_legacy: bool = False,
    purge_quarantine: bool = False,
) -> MigrationPlan:
    """Prepare and fingerprint the complete migration scope without mutations."""
    env_candidates = tuple(_absolute(path) for path in get_env_file_candidates())
    env_paths = tuple(path for path in env_candidates if os.path.lexists(path))
    env_snapshots, unsafe_env_files = _load_dotenv_snapshots(env_paths)
    dotenv_inventory, unsafe_inventory_paths = _load_dotenv_inventory(
        env_candidates,
        env_snapshots,
    )
    dotenv_recoveries, unsafe_recovery_paths = _load_dotenv_recoveries(
        env_candidates,
        env_snapshots,
    )
    unsafe_env_files = tuple(
        dict.fromkeys((*unsafe_env_files, *unsafe_inventory_paths, *unsafe_recovery_paths))
    )
    env_values = {path: snapshot.values for path, snapshot in env_snapshots.items()}
    runtime_config = _resolve_migration_config(config, env_values)
    store = TokenStore(runtime_config.garmintokens)
    resolved_legacy_path = _resolve_legacy_token_path(legacy_path, env_values)
    quarantine_path = TokenStore.quarantine_path_for(store.directory)
    migration_lock = _absolute(DEFAULT_MIGRATION_LOCK_FILE)
    blockers = [
        f"Refusing unsafe, unreadable, oversized, or non-UTF-8 dotenv path: {path}"
        for path in unsafe_env_files
    ]
    interrupted_writes: tuple[Path, ...] = ()
    try:
        store.validate_dedicated_store()
        interrupted_writes = store.interrupted_writes()
    except TokenStoreError as exc:
        blockers.append(str(exc))
    if interrupted_writes:
        blockers.append(
            "Interrupted token writes require a successful new authentication or refresh "
            "before migration: " + ", ".join(str(path) for path in interrupted_writes)
        )

    try:
        canonical = store.read_snapshot(require_secure_permissions=False)
    except TokenStoreError:
        canonical = None

    dotenv_cleanups: list[DotenvCleanupPlan] = []
    local_env = _absolute(Path.cwd() / LOCAL_ENV_FILE)
    for path, snapshot in env_snapshots.items():
        absolute_path = _absolute(path)
        content = snapshot.content
        keys = snapshot.legacy_key_spellings
        if not keys:
            continue
        if absolute_path == local_env and not include_local_env:
            blockers.append(
                f"Local dotenv cleanup requires explicit --include-local-env: {absolute_path}"
            )
            continue
        try:
            _assert_integrity_protected_parent(absolute_path.parent)
            protection = _capture_protection(absolute_path)
        except (OSError, TokenStoreError) as exc:
            blockers.append(f"Could not verify dotenv protection for {absolute_path}: {exc}")
            continue
        if os.name != "nt" and protection.owner_id != os.geteuid():
            blockers.append(f"Dotenv is owned by another user: {absolute_path}")
            continue
        if os.name != "nt" and protection.posix_xattrs:
            blockers.append(
                "Dotenv has POSIX ACLs or extended attributes that cannot be safely "
                f"preserved: {absolute_path}"
            )
            continue
        if os.name != "nt" and not _can_reproduce_group(
            protection,
            absolute_path.parent,
        ):
            blockers.append(f"Dotenv owning group cannot be reproduced safely: {absolute_path}")
            continue
        if os.name == "nt" and protection.windows_acl is not None:
            from .windows_acl import current_user_sid

            if protection.windows_acl.owner_sid != current_user_sid():
                blockers.append(f"Dotenv is owned by another Windows SID: {absolute_path}")
                continue
        dotenv_cleanups.append(
            DotenvCleanupPlan(
                path=absolute_path,
                keys=keys,
                fingerprint=_fingerprint(content),
                replacement=_remove_dotenv_keys(content, keys),
                protection=protection,
            )
        )

    legacy_fingerprint: str | None = None
    legacy_protection: FileProtection | None = None
    possible_disclosure_paths: list[Path] = []
    legacy_exists = os.path.lexists(resolved_legacy_path)
    quarantine_exists = os.path.lexists(quarantine_path)
    default_legacy_path = _absolute(DEFAULT_LEGACY_TOKEN_FILE)
    if legacy_exists:
        if not TokenStore.external_artifact_permissions_secure(resolved_legacy_path):
            possible_disclosure_paths.append(resolved_legacy_path)
        reserved_paths = {
            "global migration lock": migration_lock,
            "canonical token": store.token_file,
            "token-store lock": store.directory / LOCK_FILENAME,
            "legacy quarantine": quarantine_path,
        }
        reserved_paths.update(
            {
                f"dotenv recovery for {candidate}": _dotenv_recovery_path(candidate)
                for candidate in env_candidates
            }
        )
        for purpose, reserved_path in reserved_paths.items():
            if _same_path(resolved_legacy_path, reserved_path):
                blockers.append(
                    f"Legacy token path aliases reserved {purpose}: {resolved_legacy_path}"
                )
        if resolved_legacy_path != default_legacy_path and not allow_custom_legacy:
            blockers.append(
                f"Custom legacy target requires explicit --allow-custom-legacy: "
                f"{resolved_legacy_path}"
            )
        try:
            _assert_integrity_protected_parent(resolved_legacy_path.parent)
            TokenStore._assert_current_owner(resolved_legacy_path)
            legacy_snapshot = TokenStore.read_external_artifact(resolved_legacy_path)
            legacy_fingerprint = legacy_snapshot.fingerprint
            legacy_protection = _capture_protection(resolved_legacy_path)
        except TokenStoreError as exc:
            blockers.append(f"Refusing unrecognized legacy token {resolved_legacy_path}: {exc}")
        if canonical is not None:
            try:
                if os.stat(resolved_legacy_path).st_dev != os.stat(store.directory).st_dev:
                    blockers.append(
                        "Legacy token and protected quarantine are on different filesystems; "
                        "automatic atomic migration is unavailable"
                    )
            except OSError as exc:
                blockers.append(f"Could not verify legacy filesystem boundary: {exc}")
        if quarantine_exists:
            blockers.append(
                f"Both legacy and quarantine artifacts exist; resolve manually: {quarantine_path}"
            )

    quarantine_fingerprint: str | None = None
    quarantine_protection: FileProtection | None = None
    repair_quarantine_permissions = False
    if quarantine_exists:
        if not TokenStore.external_artifact_permissions_secure(quarantine_path):
            possible_disclosure_paths.append(quarantine_path)
        try:
            _assert_integrity_protected_parent(quarantine_path.parent)
            TokenStore._assert_current_owner(quarantine_path)
            quarantine_snapshot = TokenStore.read_external_artifact(quarantine_path)
            quarantine_fingerprint = quarantine_snapshot.fingerprint
            quarantine_protection = _capture_protection(quarantine_path)
            repair_quarantine_permissions = not TokenStore.external_artifact_permissions_secure(
                quarantine_path
            )
        except TokenStoreError as exc:
            blockers.append(f"Refusing unrecognized quarantine {quarantine_path}: {exc}")

    if purge_quarantine and legacy_exists:
        blockers.append("Quarantine purge is separate; migrate the original legacy file first")

    repair_permissions = False
    if canonical is not None and not store.permissions_secure():
        try:
            store.validate_permission_repair()
            repair_permissions = True
        except TokenStoreError as exc:
            blockers.append(f"Canonical token permissions cannot be repaired safely: {exc}")
    if canonical is None:
        blockers.append(
            "A usable canonical token is required before authentication migration can "
            "complete. Run 'garmin-connect-mcp auth' first."
        )

    return MigrationPlan(
        migration_lock=migration_lock,
        canonical_token=store.token_file,
        canonical_fingerprint=canonical.fingerprint if canonical else None,
        dotenv_cleanups=tuple(dotenv_cleanups),
        repair_permissions=repair_permissions,
        legacy_path=resolved_legacy_path if legacy_exists else None,
        legacy_fingerprint=legacy_fingerprint,
        quarantine_path=quarantine_path,
        quarantine_fingerprint=quarantine_fingerprint,
        purge_quarantine=purge_quarantine,
        blockers=tuple(dict.fromkeys(blockers)),
        dotenv_recoveries=dotenv_recoveries,
        dotenv_inventory=dotenv_inventory,
        legacy_candidate_path=resolved_legacy_path,
        legacy_expected_exists=legacy_exists,
        quarantine_expected_exists=quarantine_exists,
        legacy_protection=legacy_protection,
        quarantine_protection=quarantine_protection,
        repair_quarantine_permissions=repair_quarantine_permissions,
        possible_disclosure_paths=tuple(dict.fromkeys(possible_disclosure_paths)),
    )


def apply_migration_plan(
    plan: MigrationPlan,
    confirm: Callable[[str], bool],
) -> MigrationResult:
    """Apply an unchanged prepared plan after explicit confirmation."""
    if plan.blockers:
        raise TokenStoreError("Migration plan is blocked: " + "; ".join(plan.blockers))
    if plan.has_changes and not confirm("Apply exactly the migration plan shown above?"):
        return MigrationResult(
            False,
            False,
            plan.quarantine_path,
            False,
            (),
            False,
            possible_disclosure_paths=plan.possible_disclosure_paths,
        )

    store = TokenStore(plan.canonical_token.parent)
    with store.migration_lock(plan.migration_lock):
        interrupted_writes = store.interrupted_writes()
        if interrupted_writes:
            raise TokenStoreError(
                "Interrupted token writes appeared after plan creation; rerun authentication "
                "or refresh before migration"
            )
        canonical = store.read_snapshot(require_secure_permissions=False)
        if canonical.fingerprint != plan.canonical_fingerprint:
            raise TokenStoreError("Canonical token changed after plan creation; rerun migration")

        env_candidates = tuple(entry.path for entry in plan.dotenv_inventory)
        env_paths = tuple(path for path in env_candidates if os.path.lexists(path))
        env_snapshots, unsafe_env_files = _load_dotenv_snapshots(env_paths)
        current_inventory, unsafe_inventory_paths = _load_dotenv_inventory(
            env_candidates,
            env_snapshots,
        )
        unsafe_env_files = tuple(dict.fromkeys((*unsafe_env_files, *unsafe_inventory_paths)))
        if unsafe_env_files:
            raise TokenStoreError(
                "Dotenv inventory became unsafe after plan creation: "
                + ", ".join(str(path) for path in unsafe_env_files)
            )
        if current_inventory != plan.dotenv_inventory:
            raise TokenStoreError("Dotenv inventory changed after plan creation; rerun migration")
        current_recoveries, unsafe_recovery_paths = _load_dotenv_recoveries(
            env_candidates,
            env_snapshots,
        )
        if unsafe_recovery_paths:
            raise TokenStoreError(
                "Dotenv recovery state became unsafe after plan creation: "
                + ", ".join(str(path) for path in unsafe_recovery_paths)
            )
        if current_recoveries != plan.dotenv_recoveries:
            raise TokenStoreError(
                "Interrupted dotenv recovery state changed after plan creation; rerun migration"
            )

        if plan.legacy_candidate_path is None:
            raise TokenStoreError("Migration plan has no legacy-path inventory; rerun migration")
        if os.path.lexists(plan.legacy_candidate_path) != plan.legacy_expected_exists:
            raise TokenStoreError("Legacy token existence changed after plan creation")
        if plan.quarantine_path is None:
            raise TokenStoreError(
                "Migration plan has no quarantine-path inventory; rerun migration"
            )
        if os.path.lexists(plan.quarantine_path) != plan.quarantine_expected_exists:
            raise TokenStoreError("Legacy quarantine existence changed after plan creation")

        snapshots: dict[Path, EnvSnapshot] = {}
        for cleanup in plan.dotenv_cleanups:
            content = _read_dotenv_snapshot(cleanup.path).content
            if _fingerprint(content) != cleanup.fingerprint:
                raise TokenStoreError(f"Dotenv changed after plan creation: {cleanup.path}")
            if _capture_protection(cleanup.path) != cleanup.protection:
                raise TokenStoreError(
                    f"Dotenv protection changed after plan creation: {cleanup.path}"
                )
            snapshots[cleanup.path] = (content, cleanup.protection)

        if plan.legacy_expected_exists and plan.legacy_path is not None:
            _assert_integrity_protected_parent(plan.legacy_path.parent)
            TokenStore._assert_current_owner(plan.legacy_path)
            if _capture_protection(plan.legacy_path) != plan.legacy_protection:
                raise TokenStoreError(
                    "Legacy token protection changed after plan creation; rerun migration"
                )
            current_legacy = TokenStore.read_external_artifact(plan.legacy_path)
            if current_legacy.fingerprint != plan.legacy_fingerprint:
                raise TokenStoreError("Legacy token changed after plan creation; rerun migration")

        if plan.quarantine_expected_exists and plan.quarantine_path is not None:
            _assert_integrity_protected_parent(plan.quarantine_path.parent)
            TokenStore._assert_current_owner(plan.quarantine_path)
            if _capture_protection(plan.quarantine_path) != plan.quarantine_protection:
                raise TokenStoreError(
                    "Quarantine protection changed after plan creation; rerun migration"
                )
            current_quarantine = TokenStore.read_external_artifact(plan.quarantine_path)
            if current_quarantine.fingerprint != plan.quarantine_fingerprint:
                raise TokenStoreError("Quarantine changed after plan creation; rerun migration")

        if not plan.has_changes:
            _assert_completed_migration_state(plan, store)
            return MigrationResult(
                False,
                False,
                plan.quarantine_path,
                False,
                (),
                False,
                possible_disclosure_paths=plan.possible_disclosure_paths,
            )

        cleaned_env_files: list[Path] = []
        mutated_env_fingerprints: dict[Path, str] = {}
        repaired_permissions = False
        repaired_quarantine_permissions = False
        quarantined = False
        purged = False
        reconciled_dotenv_writes: list[Path] = []
        try:
            for recovery in current_recoveries:
                _reconcile_dotenv_recovery(recovery)
                reconciled_dotenv_writes.append(recovery.temporary_path)

            for cleanup in plan.dotenv_cleanups:
                replacement_fingerprint = _fingerprint(cleanup.replacement)
                # Record the only state rollback is authorized to replace. If
                # the write fails before rename, the fingerprint will not match.
                mutated_env_fingerprints[cleanup.path] = replacement_fingerprint
                _atomic_replace_protected_file(
                    cleanup.path,
                    cleanup.replacement,
                    cleanup.protection,
                    expected_fingerprint=cleanup.fingerprint,
                )
                cleaned_env_files.append(cleanup.path)

            if plan.repair_permissions:
                store.enforce_permissions()
                repaired_canonical = store.read_snapshot()
                if repaired_canonical.fingerprint != plan.canonical_fingerprint:
                    raise TokenStoreError(
                        "Canonical token changed during permission repair; rerun migration"
                    )
                repaired_permissions = True

            if plan.repair_quarantine_permissions:
                if plan.quarantine_path is None or plan.quarantine_fingerprint is None:
                    raise TokenStoreError("Quarantine repair plan has no fingerprinted artifact")
                TokenStore.harden_external_artifact(
                    plan.quarantine_path,
                    expected_fingerprint=plan.quarantine_fingerprint,
                )
                repaired_quarantine_permissions = True

            # The irreversible scope is last. Migration is an atomic rename to
            # a recovery file inside the protected canonical directory.
            if plan.legacy_path is not None and plan.quarantine_path is not None:
                _assert_integrity_protected_parent(plan.legacy_path.parent)
                TokenStore.harden_external_artifact(
                    plan.legacy_path,
                    expected_fingerprint=plan.legacy_fingerprint or "",
                )
                _assert_integrity_protected_parent(plan.legacy_path.parent)
                atomic_replace_file(plan.legacy_path, plan.quarantine_path)
                try:
                    _sync_directories(plan.legacy_path.parent, plan.quarantine_path.parent)
                    moved = TokenStore.read_external_artifact(plan.quarantine_path)
                    if moved.fingerprint != plan.legacy_fingerprint:
                        raise TokenStoreError("Quarantined token failed post-move verification")
                except Exception:
                    atomic_replace_file(plan.quarantine_path, plan.legacy_path)
                    _sync_directories(plan.quarantine_path.parent, plan.legacy_path.parent)
                    raise
                quarantined = True

            # Purge is available only in a later, separately planned invocation.
            if plan.purge_quarantine and plan.quarantine_path is not None:
                TokenStore._assert_current_owner(plan.quarantine_path)
                quarantine = TokenStore.read_external_artifact(plan.quarantine_path)
                if quarantine.fingerprint != plan.quarantine_fingerprint:
                    raise TokenStoreError("Quarantine changed immediately before purge")
                if not TokenStore.external_artifact_permissions_secure(plan.quarantine_path):
                    raise TokenStoreError("Quarantine is not owner-only immediately before purge")
                plan.quarantine_path.unlink()
                _sync_directory(plan.quarantine_path.parent)
                purged = True

            _assert_completed_migration_state(plan, store)
        except Exception as exc:
            _restore_env_files(snapshots, mutated_env_fingerprints)
            raise TokenStoreError(f"Legacy authentication migration failed: {exc}") from exc

    return MigrationResult(
        changed=True,
        quarantined_legacy_token=quarantined,
        quarantine_path=plan.quarantine_path,
        purged_quarantine=purged,
        cleaned_env_files=tuple(cleaned_env_files),
        repaired_permissions=repaired_permissions,
        reconciled_dotenv_writes=tuple(reconciled_dotenv_writes),
        repaired_quarantine_permissions=repaired_quarantine_permissions,
        possible_disclosure_paths=plan.possible_disclosure_paths,
    )


def migrate_auth_state(
    confirm: Callable[[str], bool],
    config: GarminConfig | None = None,
    legacy_path: Path | None = None,
    *,
    include_local_env: bool = False,
    allow_custom_legacy: bool = False,
    purge_quarantine: bool = False,
) -> MigrationResult:
    """Compatibility facade for callers that do not need to render the plan."""
    plan = build_migration_plan(
        config,
        legacy_path,
        include_local_env=include_local_env,
        allow_custom_legacy=allow_custom_legacy,
        purge_quarantine=purge_quarantine,
    )
    return apply_migration_plan(plan, confirm)


def _remove_dotenv_keys(content: bytes, keys: tuple[str, ...]) -> bytes:
    """Render one formatting-preserving dotenv edit in memory."""
    try:
        source = content.decode("utf-8")
    except UnicodeError as exc:
        raise TokenStoreError("Dotenv file is not valid UTF-8") from exc
    removed = set(keys)
    rendered = "".join(
        binding.original.string
        for binding in parse_stream(io.StringIO(source))
        if binding.key not in removed
    )
    return rendered.encode("utf-8")


def _capture_protection(path: Path) -> FileProtection:
    try:
        metadata = TokenStore._assert_regular_non_redirect(path)
        if os.name == "nt":
            from .windows_acl import read_acl_descriptor

            return FileProtection(
                mode=stat.S_IMODE(metadata.st_mode),
                owner_id=None,
                windows_acl=read_acl_descriptor(path),
            )
        return FileProtection(
            mode=stat.S_IMODE(metadata.st_mode),
            owner_id=metadata.st_uid,
            windows_acl=None,
            posix_xattrs=_list_posix_xattrs(path),
            group_id=metadata.st_gid,
        )
    except TokenStoreError:
        raise
    except OSError as exc:
        raise TokenStoreError(f"Could not capture artifact protection: {path}") from exc


def _atomic_replace_protected_file(
    path: Path,
    content: bytes,
    protection: FileProtection,
    *,
    expected_fingerprint: str,
) -> None:
    """CAS-replace one dotenv while preserving protection before visibility."""
    _assert_integrity_protected_parent(path.parent)
    if not _safe_regular_file(path):
        raise TokenStoreError(f"Dotenv path became unsafe: {path}")
    current = _read_dotenv_snapshot(path).content
    if _fingerprint(current) != expected_fingerprint:
        raise TokenStoreError(f"Dotenv changed before atomic replace: {path}")
    if _capture_protection(path) != protection:
        raise TokenStoreError(f"Dotenv protection changed before atomic replace: {path}")

    temporary_path: Path | None = _dotenv_recovery_path(path)
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary_path, flags, 0o600)
    except FileExistsError as exc:
        raise TokenStoreError(
            "Interrupted dotenv migration requires confirmed recovery before another write: "
            f"{temporary_path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            opened_metadata = _assert_open_file_identity(stream.fileno(), temporary_path)
            if opened_metadata.st_size != 0:
                raise TokenStoreError(f"Temporary dotenv was not created empty: {path}")
            # The parent directory may be broadly accessible. Install and
            # verify the captured protection while the temp is still empty so
            # no retained dotenv secret is ever visible under inherited ACLs.
            _apply_protection(temporary_path, protection)
            if _capture_protection(temporary_path) != protection:
                raise TokenStoreError(f"Temporary dotenv protection verification failed: {path}")
            _assert_open_file_identity(stream.fileno(), temporary_path)
            # Persist the protected empty generation and its deterministic
            # directory entry before any retained secret bytes are written.
            os.fsync(stream.fileno())
            _sync_directory(path.parent)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            _assert_open_file_identity(stream.fileno(), temporary_path)
            if _capture_protection(temporary_path) != protection:
                raise TokenStoreError(f"Temporary dotenv protection changed while writing: {path}")
            _assert_open_file_identity(stream.fileno(), temporary_path)

        # Revalidate immediately before making the prepared file visible.
        _assert_integrity_protected_parent(path.parent)
        if _fingerprint(_read_dotenv_snapshot(path).content) != expected_fingerprint:
            raise TokenStoreError(f"Dotenv changed during atomic replace: {path}")
        if _capture_protection(path) != protection:
            raise TokenStoreError(f"Dotenv protection changed during atomic replace: {path}")
        atomic_replace_file(temporary_path, path)
        temporary_path = None
        _sync_directory(path.parent)

        if _fingerprint(_read_dotenv_snapshot(path).content) != _fingerprint(content):
            raise TokenStoreError(f"Dotenv content verification failed after replace: {path}")
        # ReplaceFileW can merge inherited ACE bookkeeping from the old target.
        # Reapply the captured descriptor; the replacement already had the same
        # effective principals, so this never creates a wider visibility window.
        _apply_protection(path, protection)
        if _capture_protection(path) != protection:
            raise TokenStoreError(f"Dotenv protection verification failed after replace: {path}")
        _sync_regular_file(path)
        _sync_directory(path.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
                _sync_directory(path.parent)
            except OSError as exc:
                raise TokenStoreError(
                    f"Could not durably remove temporary dotenv: {temporary_path}"
                ) from exc


def _assert_open_file_identity(descriptor: int, path: Path) -> os.stat_result:
    """Verify an open single-link temp is still the file named by its path."""
    opened_metadata = os.fstat(descriptor)
    current_metadata = TokenStore._assert_regular_non_redirect(path)
    if (
        not stat.S_ISREG(opened_metadata.st_mode)
        or opened_metadata.st_nlink != 1
        or not TokenStore._same_file(opened_metadata, current_metadata)
    ):
        raise TokenStoreError(f"Temporary dotenv changed while open: {path}")
    return opened_metadata


def _sync_regular_file(path: Path) -> None:
    """Durably persist file metadata on platforms that require explicit fsync."""
    if os.name == "nt":
        return
    metadata = TokenStore._assert_regular_non_redirect(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if not TokenStore._same_file(metadata, opened_metadata):
            raise TokenStoreError(f"File changed before durability sync: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_directories(*paths: Path) -> None:
    """Sync every distinct directory involved in a cross-directory rename."""
    synced: set[str] = set()
    for path in paths:
        absolute = _absolute(path)
        key = os.path.normcase(str(absolute))
        if key in synced:
            continue
        _sync_directory(absolute)
        synced.add(key)


def _sync_directory(path: Path) -> None:
    """Durably persist rename/unlink directory entries on POSIX."""
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    absolute = _absolute(path)
    TokenStore._assert_safe_path_chain(absolute)
    metadata = os.lstat(absolute)
    if TokenStore._is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise TokenStoreError(f"Durability sync target is not a safe directory: {absolute}")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if not TokenStore._same_file(metadata, opened_metadata):
            raise TokenStoreError(f"Directory changed before durability sync: {absolute}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply_protection(path: Path, protection: FileProtection) -> None:
    if os.name == "nt":
        from .windows_acl import apply_acl_descriptor

        if protection.windows_acl is None:
            raise TokenStoreError(f"Missing Windows ACL snapshot for {path}")
        apply_acl_descriptor(path, protection.windows_acl)
        return
    _assert_integrity_protected_parent(path.parent)
    metadata = TokenStore._assert_regular_non_redirect(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if not TokenStore._same_file(metadata, opened_metadata):
            raise TokenStoreError(f"File changed before protection was applied: {path}")
        _apply_open_file_protection(descriptor, path, protection)
    finally:
        os.close(descriptor)


def _apply_open_file_protection(
    descriptor: int,
    path: Path,
    protection: FileProtection,
) -> None:
    """Reproduce POSIX owner/group/mode on an already verified open inode."""
    if protection.owner_id != _posix_effective_uid():
        raise TokenStoreError(f"Refusing protection owned by another user for {path}")
    if protection.group_id is None:
        raise TokenStoreError(f"Missing POSIX owning group snapshot for {path}")
    if protection.posix_xattrs:
        raise TokenStoreError(f"Refusing to discard POSIX ACLs or attributes for {path}")
    opened_metadata = os.fstat(descriptor)
    if opened_metadata.st_uid != _posix_effective_uid() or opened_metadata.st_nlink != 1:
        raise TokenStoreError(f"Refusing to protect a foreign or linked file: {path}")
    if opened_metadata.st_gid != protection.group_id:
        os.fchown(  # pyright: ignore[reportAttributeAccessIssue]
            descriptor,
            -1,
            protection.group_id,
        )
    os.fchmod(  # pyright: ignore[reportAttributeAccessIssue]
        descriptor,
        protection.mode,
    )
    hardened_metadata = os.fstat(descriptor)
    if (
        hardened_metadata.st_uid != protection.owner_id
        or hardened_metadata.st_gid != protection.group_id
        or stat.S_IMODE(hardened_metadata.st_mode) != protection.mode
        or hardened_metadata.st_nlink != 1
    ):
        raise TokenStoreError(f"Could not reproduce exact POSIX protection for {path}")


def _assert_integrity_protected_parent(path: Path) -> None:
    """Use the canonical cross-platform parent-entry integrity contract."""
    TokenStore.assert_integrity_protected_parent(path)


def _can_reproduce_group(protection: FileProtection, parent: Path) -> bool:
    """Check whether a new inode can retain the dotenv's exact POSIX group."""
    if protection.group_id is None:
        return False
    parent_metadata = os.lstat(parent)
    inherited_group = (
        parent_metadata.st_gid
        if stat.S_IMODE(parent_metadata.st_mode) & stat.S_ISGID
        else _posix_effective_group_id()
    )
    return bool(
        protection.group_id == inherited_group
        or _posix_effective_uid() == 0
        or protection.group_id == _posix_effective_group_id()
        or protection.group_id in _posix_supplementary_groups()
    )


def _posix_effective_uid() -> int:
    return os.geteuid()  # pyright: ignore[reportAttributeAccessIssue]


def _posix_effective_group_id() -> int:
    return os.getegid()  # pyright: ignore[reportAttributeAccessIssue]


def _posix_supplementary_groups() -> list[int]:
    return os.getgroups()  # pyright: ignore[reportAttributeAccessIssue]


def _restore_env_files(
    snapshots: dict[Path, EnvSnapshot],
    expected_current_fingerprints: dict[Path, str],
) -> None:
    """CAS-restore dotenv snapshots without overwriting concurrent changes."""
    failures: list[str] = []
    for path, (content, protection) in snapshots.items():
        expected = expected_current_fingerprints.get(path)
        if expected is None:
            continue
        try:
            current_fingerprint = _fingerprint(_read_dotenv_snapshot(path).content)
            if current_fingerprint == _fingerprint(content):
                # The attempted write failed before becoming visible.
                continue
            if current_fingerprint != expected:
                raise TokenStoreError(
                    f"Refusing to overwrite concurrently changed dotenv during rollback: {path}"
                )
            _atomic_replace_protected_file(
                path,
                content,
                protection,
                expected_fingerprint=expected,
            )
        except (OSError, TokenStoreError) as exc:
            failures.append(f"{path}: {exc}")

    if failures:
        raise TokenStoreError("Could not restore dotenv files: " + "; ".join(failures))


def _resolve_migration_config(
    config: GarminConfig | None,
    env_values: dict[Path, dict[str, str | None]],
) -> GarminConfig:
    """Resolve legacy dotenv token paths only for explicit migration commands."""
    if config is not None:
        return config

    runtime_config = load_config()
    runtime_found, _runtime_value = _case_insensitive_lookup(os.environ, "GARMINTOKENS")
    if runtime_found:
        return runtime_config

    for path in reversed(tuple(env_values)):
        legacy_found, legacy_token_store = _case_insensitive_lookup(
            env_values[path],
            "GARMINTOKENS",
        )
        if legacy_found:
            if legacy_token_store is None:
                return runtime_config
            return GarminConfig(garmintokens=legacy_token_store)
    return runtime_config


def _resolve_legacy_token_path(
    explicit_path: Path | None,
    env_values: dict[Path, dict[str, str | None]],
) -> Path:
    """Resolve the deprecated token path from the same safe dotenv snapshots."""
    if explicit_path is not None:
        return _absolute(explicit_path)

    runtime_found, runtime_value = _case_insensitive_lookup(
        os.environ,
        "GARMINTOKENS_BASE64",
    )
    if runtime_found:
        return _absolute(Path(runtime_value) if runtime_value else DEFAULT_LEGACY_TOKEN_FILE)

    for path in reversed(tuple(env_values)):
        legacy_found, legacy_value = _case_insensitive_lookup(
            env_values[path],
            "GARMINTOKENS_BASE64",
        )
        if legacy_found:
            return _absolute(Path(legacy_value) if legacy_value else DEFAULT_LEGACY_TOKEN_FILE)
    return _absolute(DEFAULT_LEGACY_TOKEN_FILE)


def _load_dotenv_snapshots(
    paths: tuple[Path, ...],
) -> tuple[dict[Path, DotenvSnapshot], tuple[Path, ...]]:
    """Load bounded dotenv snapshots and classify every unsafe read as data."""
    snapshots: dict[Path, DotenvSnapshot] = {}
    failures: list[Path] = []
    for path in paths:
        absolute_path = _absolute(path)
        try:
            snapshots[absolute_path] = _read_dotenv_snapshot(absolute_path)
        except (OSError, TokenStoreError):
            failures.append(absolute_path)
    return snapshots, tuple(failures)


def _load_dotenv_inventory(
    candidates: tuple[Path, ...],
    snapshots: dict[Path, DotenvSnapshot],
) -> tuple[tuple[DotenvInventoryEntry, ...], tuple[Path, ...]]:
    """Capture every known dotenv candidate, including its protected absence."""
    inventory: list[DotenvInventoryEntry] = []
    failures: list[Path] = []
    for candidate in dict.fromkeys(_absolute(path) for path in candidates):
        try:
            _assert_integrity_protected_parent(candidate.parent)
            if not os.path.lexists(candidate):
                inventory.append(
                    DotenvInventoryEntry(
                        path=candidate,
                        exists=False,
                        fingerprint=None,
                        protection=None,
                        legacy_key_spellings=(),
                    )
                )
                continue
            snapshot = snapshots.get(candidate)
            if snapshot is None:
                raise TokenStoreError(f"Dotenv inventory could not read: {candidate}")
            protection = _capture_protection(candidate)
            if os.name == "nt":
                from .windows_acl import file_integrity_is_protected

                if not file_integrity_is_protected(candidate):
                    raise TokenStoreError(f"Dotenv grants untrusted mutation rights: {candidate}")
            elif protection.owner_id != _posix_effective_uid() or protection.mode & 0o022:
                raise TokenStoreError(
                    f"Dotenv ownership or write permissions are unsafe: {candidate}"
                )
            inventory.append(
                DotenvInventoryEntry(
                    path=candidate,
                    exists=True,
                    fingerprint=_fingerprint(snapshot.content),
                    protection=protection,
                    legacy_key_spellings=snapshot.legacy_key_spellings,
                )
            )
        except (OSError, TokenStoreError):
            failures.append(candidate)
    return tuple(inventory), tuple(failures)


def _assert_completed_migration_state(plan: MigrationPlan, store: TokenStore) -> None:
    """Refuse success unless the complete local auth inventory is in the planned end state."""
    canonical = store.read_snapshot()
    if canonical.fingerprint != plan.canonical_fingerprint:
        raise TokenStoreError("Canonical token changed before migration completed")
    interrupted_writes = store.interrupted_writes()
    if interrupted_writes:
        raise TokenStoreError(
            "Interrupted token writes appeared before migration completed: "
            + ", ".join(str(path) for path in interrupted_writes)
        )

    env_candidates = tuple(entry.path for entry in plan.dotenv_inventory)
    env_paths = tuple(path for path in env_candidates if os.path.lexists(path))
    env_snapshots, unsafe_env_files = _load_dotenv_snapshots(env_paths)
    _inventory, unsafe_inventory_paths = _load_dotenv_inventory(
        env_candidates,
        env_snapshots,
    )
    unsafe_env_files = tuple(dict.fromkeys((*unsafe_env_files, *unsafe_inventory_paths)))
    if unsafe_env_files:
        raise TokenStoreError(
            "Dotenv inventory became unsafe before migration completed: "
            + ", ".join(str(path) for path in unsafe_env_files)
        )
    remaining_dotenv_keys = {
        path: snapshot.legacy_key_spellings
        for path, snapshot in env_snapshots.items()
        if snapshot.legacy_key_spellings
    }
    if remaining_dotenv_keys:
        raise TokenStoreError(
            "Legacy dotenv bindings remain after migration: "
            + ", ".join(str(path) for path in remaining_dotenv_keys)
        )
    recoveries, unsafe_recovery_paths = _load_dotenv_recoveries(
        env_candidates,
        env_snapshots,
    )
    if unsafe_recovery_paths or recoveries:
        paths = (*unsafe_recovery_paths, *(item.temporary_path for item in recoveries))
        raise TokenStoreError(
            "Interrupted dotenv state remains after migration: "
            + ", ".join(str(path) for path in paths)
        )

    if plan.legacy_candidate_path is None:
        raise TokenStoreError("Migration plan has no legacy-path completion inventory")
    if os.path.lexists(plan.legacy_candidate_path):
        raise TokenStoreError(f"Legacy token remains after migration: {plan.legacy_candidate_path}")
    if plan.quarantine_path is None:
        raise TokenStoreError("Migration plan has no quarantine-path completion inventory")

    expected_quarantine_fingerprint: str | None
    if plan.purge_quarantine:
        expected_quarantine_fingerprint = None
    elif plan.legacy_fingerprint is not None:
        expected_quarantine_fingerprint = plan.legacy_fingerprint
    else:
        expected_quarantine_fingerprint = plan.quarantine_fingerprint
    if expected_quarantine_fingerprint is None:
        if os.path.lexists(plan.quarantine_path):
            raise TokenStoreError(
                f"Unexpected legacy quarantine remains after migration: {plan.quarantine_path}"
            )
    else:
        quarantine = TokenStore.read_external_artifact(plan.quarantine_path)
        if quarantine.fingerprint != expected_quarantine_fingerprint:
            raise TokenStoreError("Legacy quarantine changed before migration completed")
        if not TokenStore.external_artifact_permissions_secure(plan.quarantine_path):
            raise TokenStoreError("Legacy quarantine is not owner-only after migration")


def _load_dotenv_recoveries(
    candidates: tuple[Path, ...],
    snapshots: dict[Path, DotenvSnapshot],
) -> tuple[tuple[DotenvRecoveryPlan, ...], tuple[Path, ...]]:
    """Inventory only exact transaction-owned dotenv recovery paths."""
    recoveries: list[DotenvRecoveryPlan] = []
    failures: list[Path] = []
    for candidate in dict.fromkeys(_absolute(path) for path in candidates):
        temporary_path = _dotenv_recovery_path(candidate)
        if not os.path.lexists(temporary_path):
            continue
        target_snapshot = snapshots.get(candidate)
        if target_snapshot is None:
            failures.append(temporary_path)
            continue
        try:
            recoveries.append(_inspect_dotenv_recovery(candidate, target_snapshot))
        except (OSError, TokenStoreError):
            failures.append(temporary_path)
    return tuple(recoveries), tuple(failures)


def _inspect_dotenv_recovery(
    target_path: Path,
    target_snapshot: DotenvSnapshot,
) -> DotenvRecoveryPlan:
    """Validate one exact recovery as empty or an original/cleaned counterpart."""
    target_path = _absolute(target_path)
    temporary_path = _dotenv_recovery_path(target_path)
    if target_snapshot.path != target_path:
        raise TokenStoreError(f"Dotenv recovery target changed: {target_path}")

    _assert_integrity_protected_parent(target_path.parent)
    target_protection = _capture_protection(target_path)
    temporary_snapshot = _read_dotenv_snapshot(temporary_path)
    temporary_protection = _capture_protection(temporary_path)
    if not _protection_owned_and_reproducible(
        target_protection,
        target_path.parent,
    ):
        raise TokenStoreError(f"Dotenv recovery target has unsupported protection: {target_path}")
    if temporary_protection != target_protection:
        raise TokenStoreError(
            f"Dotenv recovery protection does not match its target: {temporary_path}"
        )
    if temporary_snapshot.content and not _related_dotenv_generations(
        target_snapshot,
        temporary_snapshot,
    ):
        raise TokenStoreError(
            f"Dotenv recovery is not a recognized transaction generation: {temporary_path}"
        )

    return DotenvRecoveryPlan(
        target_path=target_path,
        temporary_path=temporary_path,
        target_fingerprint=_fingerprint(target_snapshot.content),
        temporary_fingerprint=_fingerprint(temporary_snapshot.content),
        protection=target_protection,
    )


def _related_dotenv_generations(
    first: DotenvSnapshot,
    second: DotenvSnapshot,
) -> bool:
    """Recognize only the two generations our dotenv cleanup can produce."""
    for original, cleaned in ((first, second), (second, first)):
        if (
            original.legacy_key_spellings
            and _remove_dotenv_keys(
                original.content,
                original.legacy_key_spellings,
            )
            == cleaned.content
        ):
            return True
    return False


def _protection_owned_and_reproducible(
    protection: FileProtection,
    parent: Path,
) -> bool:
    """Return whether migration can safely reproduce the captured protection."""
    if os.name == "nt":
        if protection.windows_acl is None:
            return False
        from .windows_acl import current_user_sid

        return protection.windows_acl.owner_sid == current_user_sid()
    return bool(
        protection.owner_id == os.geteuid()
        and not protection.posix_xattrs
        and _can_reproduce_group(protection, parent)
    )


def _reconcile_dotenv_recovery(recovery: DotenvRecoveryPlan) -> None:
    """Revalidate and durably unlink one explicitly confirmed recovery generation."""
    current_target = _read_dotenv_snapshot(recovery.target_path)
    if _inspect_dotenv_recovery(recovery.target_path, current_target) != recovery:
        raise TokenStoreError(
            f"Dotenv recovery changed immediately before reconciliation: {recovery.temporary_path}"
        )
    recovery.temporary_path.unlink()
    _sync_directory(recovery.temporary_path.parent)


def _dotenv_recovery_path(target_path: Path) -> Path:
    """Derive the sole sidecar name owned by one known dotenv target."""
    absolute = _absolute(target_path)
    identity = os.path.normcase(str(absolute)).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()
    return absolute.parent / f".garmin-connect-mcp-auth-{digest}.tmp"


def _read_dotenv_snapshot(path: Path) -> DotenvSnapshot:
    """Open one dotenv without redirects and parse exactly the bytes that were read."""
    TokenStore._assert_safe_path_chain(path.parent)
    metadata = TokenStore._assert_regular_non_redirect(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if os.name == "nt":
        from .windows_io import open_shared_read

        descriptor = open_shared_read(path)
    else:
        descriptor = os.open(path, flags)
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or not TokenStore._same_file(metadata, opened_metadata)
        ):
            raise TokenStoreError(f"Dotenv changed while opening: {path}")
        content = _read_bounded_dotenv(descriptor, path)
    finally:
        os.close(descriptor)

    try:
        source = content.decode("utf-8")
        values = dict(dotenv_values(stream=io.StringIO(source)))
    except (UnicodeError, ValueError) as exc:
        raise TokenStoreError(f"Dotenv is not valid UTF-8 configuration: {path}") from exc
    return DotenvSnapshot(
        path=path,
        content=content,
        values=values,
        legacy_key_spellings=_dotenv_key_spellings(content, LEGACY_ENV_KEYS),
    )


def _read_bounded_dotenv(descriptor: int, path: Path) -> bytes:
    """Read at most the documented dotenv safety limit from an open descriptor."""
    if os.fstat(descriptor).st_size > MAX_DOTENV_BYTES:
        raise TokenStoreError(f"Dotenv exceeds {MAX_DOTENV_BYTES} bytes: {path}")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, MAX_DOTENV_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_DOTENV_BYTES:
            raise TokenStoreError(f"Dotenv exceeds {MAX_DOTENV_BYTES} bytes: {path}")


def _dotenv_key_spellings(
    content: bytes,
    expected_keys: tuple[str, ...],
) -> tuple[str, ...]:
    """Inventory every physical legacy binding without collapsing duplicates."""
    try:
        source = content.decode("utf-8")
    except UnicodeError as exc:
        raise TokenStoreError("Dotenv file is not valid UTF-8") from exc
    expected = {key.casefold() for key in expected_keys}
    spellings = (
        binding.key
        for binding in parse_stream(io.StringIO(source))
        if binding.key is not None and binding.key.casefold() in expected
    )
    return tuple(dict.fromkeys(spellings))


def _matching_keys(
    values: Mapping[str, str | None],
    expected_keys: tuple[str, ...],
) -> tuple[str, ...]:
    """Return every actual spelling that affects legacy source precedence."""
    expected = {key.casefold() for key in expected_keys}
    return tuple(key for key in values if key.casefold() in expected)


def _list_posix_xattrs(path: Path) -> tuple[str, ...]:
    """List metadata an inode replacement cannot silently discard."""
    listxattr = getattr(os, "listxattr", None)
    if listxattr is None:
        raise TokenStoreError("This platform cannot inspect POSIX extended attributes")
    try:
        return tuple(sorted(listxattr(path, follow_symlinks=False)))
    except OSError as exc:
        raise TokenStoreError(f"Could not inspect extended attributes for {path}") from exc


def _safe_regular_file(path: Path) -> bool:
    try:
        TokenStore._assert_safe_path_chain(path.parent)
        TokenStore._assert_regular_non_redirect(path)
    except (OSError, TokenStoreError):
        return False
    return True


def _same_path(first: Path, second: Path) -> bool:
    if _absolute(first) == _absolute(second):
        return True
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
