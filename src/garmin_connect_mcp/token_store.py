"""Secure, atomic handling for the one canonical Garmin token store."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from garminconnect import Garmin, GarminConnectConnectionError

TOKEN_FILENAME = "garmin_tokens.json"
LOCK_FILENAME = ".garmin_tokens.lock"
QUARANTINE_FILENAME = ".legacy-token-quarantine.json"
MAX_TOKEN_BYTES = 64 * 1024
REQUIRED_TOKEN_FIELDS = ("di_token", "di_refresh_token", "di_client_id")
DEDICATED_STORE_ENTRIES = {TOKEN_FILENAME, LOCK_FILENAME, QUARANTINE_FILENAME}


class TokenStoreError(RuntimeError):
    """Raised when the token store cannot be used safely."""


class TokenStoreConflict(TokenStoreError):
    """Raised when another process replaced the canonical token generation."""


@dataclass(frozen=True)
class TokenSnapshot:
    """Validated token payload and its immutable content fingerprint."""

    payload: str
    fingerprint: str


@dataclass(frozen=True)
class TokenStoreAudit:
    """Security and migration state without exposing secret contents."""

    token_exists: bool
    token_permissions_secure: bool
    legacy_exists: bool
    quarantine_exists: bool
    stored_credentials: tuple[Path, ...]
    unsafe_env_files: tuple[Path, ...]
    legacy_runtime_config_files: tuple[Path, ...]
    legacy_environment_variables: tuple[str, ...]
    issues: tuple[str, ...]
    interrupted_writes: tuple[Path, ...] = ()
    interrupted_dotenv_writes: tuple[Path, ...] = ()
    legacy_permissions_secure: bool = True
    quarantine_permissions_secure: bool = True

    @property
    def needs_migration(self) -> bool:
        """Return whether legacy secrets or insecure storage remain."""
        return bool(
            self.legacy_exists
            or self.quarantine_exists
            or self.stored_credentials
            or self.unsafe_env_files
            or self.legacy_runtime_config_files
            or self.legacy_environment_variables
            or self.interrupted_writes
            or self.interrupted_dotenv_writes
            or not self.legacy_permissions_secure
            or not self.quarantine_permissions_secure
            or not self.token_permissions_secure
        )


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_held_file_locks = threading.local()


def atomic_replace_file(source: Path, target: Path) -> None:
    """Use the platform primitive compatible with shared token readers."""
    if os.name == "nt":
        from .windows_io import atomic_replace

        atomic_replace(source, target)
        return
    os.replace(source, target)


class TokenStore:
    """The only component allowed to persist canonical Garmin tokens."""

    def __init__(self, directory: Path | str):
        configured = os.fspath(directory)
        self._empty_configuration = not configured.strip()
        expanded = Path(configured).expanduser()
        absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
        self.directory = Path(os.path.abspath(absolute))

    @property
    def token_file(self) -> Path:
        """Return the canonical token file."""
        return self.directory / TOKEN_FILENAME

    def exists(self) -> bool:
        """Return whether a structurally usable canonical token exists."""
        try:
            self.read_snapshot(require_secure_permissions=False)
        except TokenStoreError:
            return False
        return True

    def read_snapshot(self, *, require_secure_permissions: bool = True) -> TokenSnapshot:
        """Securely open, validate, and fingerprint the canonical token."""
        try:
            with self._coordinated_store_access():
                payload = self._read_canonical_bytes(require_secure_permissions).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise TokenStoreError(
                f"Could not safely read canonical token: {self.token_file}"
            ) from exc
        return self.validate_payload(payload)

    def fingerprint(self) -> str:
        """Return the secure canonical generation fingerprint."""
        return self.read_snapshot().fingerprint

    def replace_payload(self, payload: str) -> TokenSnapshot:
        """Atomically replace the canonical token without a generation precondition."""
        return self._replace_payload(payload, expected_fingerprint=None)

    def compare_and_replace(self, payload: str, expected_fingerprint: str) -> TokenSnapshot:
        """Atomically replace only the canonical generation previously loaded."""
        return self._replace_payload(payload, expected_fingerprint=expected_fingerprint)

    @contextlib.contextmanager
    def migration_lock(self, global_lock_path: Path) -> Iterator[None]:
        """Serialize all auth migrations, then protect this canonical store."""
        expanded_lock = global_lock_path.expanduser()
        absolute_lock = Path(
            os.path.abspath(
                expanded_lock if expanded_lock.is_absolute() else Path.cwd() / expanded_lock
            )
        )
        self.assert_integrity_protected_parent(absolute_lock.parent)
        if not absolute_lock.parent.is_dir():
            raise TokenStoreError(
                f"Authentication migration lock directory does not exist: {absolute_lock.parent}"
            )
        self.validate_dedicated_location()
        if not self.directory.is_dir():
            raise TokenStoreError(f"Token store does not exist: {self.directory}")
        if not os.path.lexists(self.directory / LOCK_FILENAME):
            self._assert_dedicated_contents(allow_temporary_tokens=True)
        # The outer per-user lock is deliberately independent of GARMINTOKENS.
        # Migrations can share dotenv or legacy targets while using different
        # canonical stores. The inner lock still excludes a concurrent token
        # refresh in this particular canonical store.
        with self._exclusive_file_lock(absolute_lock, "authentication migration"):
            with self._exclusive_lock():
                self._assert_dedicated_contents(allow_temporary_tokens=True)
                yield

    def ensure_directory(self) -> None:
        """Create a non-redirected owner-only canonical directory."""
        self.validate_dedicated_location()
        self.assert_integrity_protected_parent(self.directory.parent)
        if os.path.lexists(self.directory):
            metadata = os.lstat(self.directory)
            if self._is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise TokenStoreError(f"Token store is not a safe directory: {self.directory}")
            if not os.path.lexists(self.directory / LOCK_FILENAME):
                self._assert_dedicated_contents(allow_temporary_tokens=True)
            if not self._directory_permissions_secure():
                raise TokenStoreError(
                    "Existing token-store directory must already be owner-only. "
                    "Use a dedicated directory or confirm an explicit auth migration repair."
                )
        else:
            self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
            self._assert_safe_path_chain(self.directory)
            self._harden_path(self.directory, directory=True)
            self._sync_parent_directory(self.directory.parent)
            if not self._directory_permissions_secure():
                raise TokenStoreError(
                    f"Could not create an owner-only token-store directory: {self.directory}"
                )

    def validate_dedicated_location(self) -> None:
        """Reject empty and broad locations that cannot be owned as an auth store."""
        normalized = os.path.normcase(str(self.directory))
        broad_locations = {
            os.path.normcase(str(Path(self.directory.anchor))),
            os.path.normcase(str(Path.home().absolute())),
            os.path.normcase(str(Path.cwd().absolute())),
        }
        if self._empty_configuration or normalized in broad_locations:
            raise TokenStoreError(
                "GARMINTOKENS must name a non-empty dedicated token-store directory, "
                "not the filesystem root, home directory, or current working directory."
            )
        self.assert_integrity_protected_parent(self.directory.parent)
        self._assert_safe_path_chain(self.directory)

    def validate_dedicated_store(self) -> None:
        """Validate both the location boundary and any existing contents."""
        with self._coordinated_store_access():
            self._assert_dedicated_contents(allow_temporary_tokens=True)

    def interrupted_writes(self) -> tuple[Path, ...]:
        """List protected interrupted-write artifacts without reading their contents."""
        with self._coordinated_store_access():
            return self._temporary_token_paths()

    def enforce_permissions(self) -> None:
        """Apply and verify owner-only controls for the directory and token."""
        self.validate_permission_repair()
        self._harden_path(self.directory, directory=True)
        self._harden_path(self.token_file, directory=False)
        if not self.permissions_secure():
            raise TokenStoreError(
                f"Could not verify owner-only token permissions: {self.token_file}"
            )

    def validate_permission_repair(self) -> None:
        """Prove an existing store can be repaired without touching foreign data."""
        self.validate_dedicated_location()
        if not self.directory.is_dir():
            raise TokenStoreError(f"Token store does not exist: {self.directory}")
        self._assert_dedicated_contents()
        directory_metadata = os.lstat(self.directory)
        if self._is_redirect(directory_metadata) or not stat.S_ISDIR(directory_metadata.st_mode):
            raise TokenStoreError(f"Token store is not a safe directory: {self.directory}")
        self._assert_current_owner(self.directory)
        self._assert_regular_non_redirect(self.token_file)
        self._assert_current_owner(self.token_file)
        if os.name != "nt" and stat.S_IMODE(directory_metadata.st_mode) & 0o022:
            raise TokenStoreError(
                "Broadly writable token-store directories cannot be repaired safely; "
                f"remove group/other write access first: {self.directory}"
            )

    def permissions_secure(self) -> bool:
        """Return whether owner, mode/ACL, and redirect checks all pass."""
        try:
            self.validate_dedicated_store()
            self._assert_regular_non_redirect(self.token_file)
            if os.name == "nt":
                from .windows_acl import acl_is_owner_only

                return acl_is_owner_only(self.directory) and acl_is_owner_only(self.token_file)

            directory_metadata = os.lstat(self.directory)
            token_metadata = os.lstat(self.token_file)
            return bool(
                directory_metadata.st_uid == os.geteuid()
                and token_metadata.st_uid == os.geteuid()
                and stat.S_IMODE(directory_metadata.st_mode) == 0o700
                and stat.S_IMODE(token_metadata.st_mode) == 0o600
            )
        except (OSError, TokenStoreError):
            return False

    @classmethod
    def read_external_artifact(cls, path: Path) -> TokenSnapshot:
        """Validate a legacy token file without following redirects."""
        absolute = Path(os.path.abspath(path.expanduser()))
        cls.assert_integrity_protected_parent(absolute.parent)
        metadata = cls._assert_regular_non_redirect(absolute)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            if os.name == "nt":
                from .windows_io import open_shared_read

                descriptor = open_shared_read(absolute)
            else:
                descriptor = os.open(absolute, flags)
        except OSError as exc:
            raise TokenStoreError(f"Could not safely open token artifact: {absolute}") from exc
        try:
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or not cls._same_file(metadata, opened_metadata)
            ):
                raise TokenStoreError(f"Token artifact changed while opening: {absolute}")
            payload = cls._read_limited(descriptor, absolute).decode("utf-8")
        except UnicodeError as exc:
            raise TokenStoreError(f"Token artifact is not UTF-8 JSON: {absolute}") from exc
        finally:
            os.close(descriptor)
        return cls.validate_payload(payload)

    @classmethod
    def external_artifact_permissions_secure(cls, path: Path) -> bool:
        """Return whether a noncanonical secret is current-owned and owner-only."""
        absolute = Path(os.path.abspath(path.expanduser()))
        try:
            cls.assert_integrity_protected_parent(absolute.parent)
            metadata = cls._assert_regular_non_redirect(absolute)
            cls._assert_current_owner(absolute)
            if os.name == "nt":
                from .windows_acl import acl_is_owner_only

                return acl_is_owner_only(absolute)
            return bool(metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o600)
        except (OSError, TokenStoreError):
            return False

    @staticmethod
    def validate_payload(payload: str) -> TokenSnapshot:
        """Validate the pinned garminconnect 0.3 token schema offline."""
        encoded = payload.encode("utf-8")
        if len(encoded) > MAX_TOKEN_BYTES:
            raise TokenStoreError("Garmin token payload exceeds the safety limit")
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TokenStoreError("Garmin token payload is not valid JSON") from exc
        if not isinstance(parsed, dict) or any(
            not isinstance(parsed.get(field), str) or not parsed[field]
            for field in REQUIRED_TOKEN_FIELDS
        ):
            raise TokenStoreError("Garmin token payload is missing required 0.3 token fields")

        # Keep the local schema honest against the actual locked dependency.
        try:
            Garmin().client.loads(payload)
        except GarminConnectConnectionError as exc:
            raise TokenStoreError("garminconnect rejected the token payload") from exc
        return TokenSnapshot(payload=payload, fingerprint=hashlib.sha256(encoded).hexdigest())

    @staticmethod
    def quarantine_path_for(canonical_directory: Path) -> Path:
        """Return the protected recovery path inside the canonical store."""
        absolute = Path(os.path.abspath(canonical_directory.expanduser()))
        return absolute / QUARANTINE_FILENAME

    @classmethod
    def harden_external_artifact(cls, path: Path, expected_fingerprint: str) -> None:
        """Harden an unchanged legacy artifact without following redirects."""
        absolute = Path(os.path.abspath(path.expanduser()))
        snapshot = cls.read_external_artifact(absolute)
        if snapshot.fingerprint != expected_fingerprint:
            raise TokenStoreConflict("Legacy token changed before permission hardening")

        if os.name == "nt":
            cls._harden_path(absolute, directory=False)
        else:
            metadata = cls._assert_regular_non_redirect(absolute)
            if metadata.st_uid != os.geteuid():
                raise TokenStoreError(f"Legacy token is owned by another user: {absolute}")
            flags = os.O_RDONLY
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(absolute, flags)
            try:
                opened_metadata = os.fstat(descriptor)
                if opened_metadata.st_nlink != 1 or not cls._same_file(
                    metadata,
                    opened_metadata,
                ):
                    raise TokenStoreConflict(
                        "Legacy token links changed before permission hardening"
                    )
                if opened_metadata.st_uid != os.geteuid():
                    raise TokenStoreError(f"Legacy token is owned by another user: {absolute}")
                os.fchmod(descriptor, 0o600)
                hardened_metadata = os.fstat(descriptor)
                if (
                    hardened_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(hardened_metadata.st_mode) != 0o600
                    or hardened_metadata.st_nlink != 1
                ):
                    raise TokenStoreError(
                        f"Could not verify durable legacy-token protection: {absolute}"
                    )
                # Persist inode mode before a later directory fsync makes the
                # quarantine rename durable.
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        verified = cls.read_external_artifact(absolute)
        if verified.fingerprint != expected_fingerprint:
            raise TokenStoreConflict("Legacy token changed during permission hardening")
        if not cls.external_artifact_permissions_secure(absolute):
            raise TokenStoreError(
                f"Could not establish owner-only token-artifact protection: {absolute}"
            )

    def _replace_payload(
        self,
        payload: str,
        expected_fingerprint: str | None,
    ) -> TokenSnapshot:
        candidate = self.validate_payload(payload)
        self.ensure_directory()
        temporary_path: Path | None = None
        durable_candidate = False
        with self._exclusive_lock():
            self._assert_dedicated_contents(allow_temporary_tokens=True)
            if expected_fingerprint is not None:
                current = self.read_snapshot()
                if current.fingerprint != expected_fingerprint:
                    raise TokenStoreConflict(
                        "The canonical Garmin token changed in another process; retry the request."
                    )
                if current.fingerprint == candidate.fingerprint:
                    return current

            self._reject_unsafe_target()
            if os.path.lexists(self.token_file):
                # ReplaceFileW intentionally preserves the target ACL. Harden
                # the old generation before replacement so an insecure legacy
                # ACL can never be visible on the new token, even briefly.
                self._harden_path(self.token_file, directory=False)
                if not self.permissions_secure():
                    raise TokenStoreError(
                        f"Could not harden existing canonical token: {self.token_file}"
                    )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".token-",
                suffix=".tmp",
                dir=self.directory,
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload.encode("utf-8"))
                    stream.flush()
                    self._harden_path(temporary_path, directory=False)
                    opened_metadata = os.fstat(stream.fileno())
                    current_metadata = self._assert_regular_non_redirect(temporary_path)
                    if not self._same_file(opened_metadata, current_metadata):
                        raise TokenStoreError(
                            f"Temporary token changed while open: {temporary_path}"
                        )
                    os.fsync(stream.fileno())
                self._reject_unsafe_target()
                self._assert_regular_non_redirect(temporary_path)
                # Persist the protected staged inode and its directory entry
                # before it becomes the sole retained recovery candidate.
                self._sync_directory()
                durable_candidate = True
                # Only a writer with another validated, durable candidate may
                # discard artifacts that could contain a newer refresh.
                discarded_interrupted = self._discard_interrupted_writes(keep=temporary_path)
                if discarded_interrupted:
                    self._sync_directory()
                self._assert_dedicated_contents(allow_temporary_tokens=True)
                atomic_replace_file(temporary_path, self.token_file)
                temporary_path = None
                # The already-hardened file is committed. Durability calls are
                # best effort so a post-rename fsync failure is not reported as
                # a failed authentication after the new token became visible.
                with contextlib.suppress(OSError, TokenStoreError):
                    self._harden_path(self.token_file, directory=False)
                    self._sync_directory()
            finally:
                if temporary_path is not None and not durable_candidate:
                    temporary_path.unlink(missing_ok=True)

            committed = self.read_snapshot()
            if committed.fingerprint != candidate.fingerprint:
                raise TokenStoreError("Canonical token verification failed after atomic replace")
            return committed

    def _read_canonical_bytes(self, require_secure_permissions: bool) -> bytes:
        self.validate_dedicated_location()
        if not self.directory.exists():
            raise TokenStoreError(f"Token store does not exist: {self.directory}")
        directory_metadata = os.lstat(self.directory)
        if self._is_redirect(directory_metadata) or not stat.S_ISDIR(directory_metadata.st_mode):
            raise TokenStoreError(f"Token store is not a safe directory: {self.directory}")
        self._assert_dedicated_contents(allow_temporary_tokens=True)
        token_metadata = self._assert_regular_non_redirect(self.token_file)

        if require_secure_permissions and not self.permissions_secure():
            raise TokenStoreError(
                "Token store permissions or ownership are unsafe. "
                "Run 'garmin-connect-mcp auth doctor' and 'auth migrate'."
            )

        if os.name != "nt":
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_descriptor = os.open(self.directory, directory_flags)
            try:
                opened_directory_metadata = os.fstat(directory_descriptor)
                if not stat.S_ISDIR(opened_directory_metadata.st_mode) or not self._same_file(
                    directory_metadata, opened_directory_metadata
                ):
                    raise TokenStoreError(
                        f"Canonical token directory changed while opening: {self.directory}"
                    )
                if require_secure_permissions and (
                    opened_directory_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(opened_directory_metadata.st_mode) != 0o700
                ):
                    raise TokenStoreError(
                        f"Canonical token directory permissions changed: {self.directory}"
                    )
                flags = os.O_RDONLY
                flags |= getattr(os, "O_NOFOLLOW", 0)
                token_descriptor = os.open(TOKEN_FILENAME, flags, dir_fd=directory_descriptor)
            finally:
                os.close(directory_descriptor)
        else:
            from .windows_io import open_shared_read

            token_descriptor = open_shared_read(self.token_file)

        try:
            opened_metadata = os.fstat(token_descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_nlink != 1
                or not self._same_file(token_metadata, opened_metadata)
            ):
                raise TokenStoreError(f"Canonical token changed while opening: {self.token_file}")
            if require_secure_permissions:
                if os.name == "nt":
                    if not self.permissions_secure():
                        raise TokenStoreError(
                            f"Canonical token ACL changed while opening: {self.token_file}"
                        )
                elif (
                    opened_metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(opened_metadata.st_mode) != 0o600
                ):
                    raise TokenStoreError(
                        f"Canonical token permissions changed while opening: {self.token_file}"
                    )
            return self._read_limited(token_descriptor, self.token_file)
        finally:
            os.close(token_descriptor)

    @contextlib.contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        with self._exclusive_file_lock(
            self.directory / LOCK_FILENAME,
            "token store",
        ):
            yield

    @contextlib.contextmanager
    def _coordinated_store_access(self) -> Iterator[None]:
        """Wait for a writer while preserving any interrupted token generation."""
        self.validate_dedicated_location()
        if not self.directory.is_dir():
            yield
            return

        # A read may need to establish an upgraded store's coordination lock.
        # Reject a foreign-owned bind mount before creating that file. Token
        # ownership is checked below only after observing whether a first
        # writer has already established the coordination boundary.
        self._assert_current_owner(self.directory)

        lock_path = self.directory / LOCK_FILENAME
        while True:
            if os.path.lexists(lock_path):
                with self._exclusive_lock():
                    self._assert_dedicated_contents(allow_temporary_tokens=True)
                    yield
                return

            try:
                if os.path.lexists(self.token_file):
                    self._assert_regular_non_redirect(self.token_file)
                    self._assert_current_owner(self.token_file)
            except (OSError, TokenStoreError):
                # A first upgraded writer creates and locks this file before
                # replacing the canonical pathname. Wait for it instead of
                # treating ReplaceFileW's transition as a missing token.
                if os.path.lexists(lock_path):
                    continue
                raise
            if os.path.lexists(lock_path):
                continue

            try:
                self._assert_dedicated_contents(allow_temporary_tokens=True)
            except TokenStoreError:
                # A first writer may have created its lock and a
                # platform-managed replacement temp during the scan.
                if os.path.lexists(lock_path):
                    continue
                raise

            # Upgraded stores can have a canonical token but no lock yet. Claim
            # the lock before reading so a first new writer cannot race us.
            if os.path.lexists(self.token_file) or self._has_temporary_tokens():
                with self._exclusive_lock():
                    self._assert_dedicated_contents(allow_temporary_tokens=True)
                    yield
                return

            # An empty store has no usable generation to protect. Recheck once
            # so a writer that just established the lock is still observed.
            if os.path.lexists(lock_path):
                continue
            self._assert_dedicated_contents()
            yield
            return

    @contextlib.contextmanager
    def _exclusive_file_lock(self, lock_path: Path, purpose: str) -> Iterator[None]:
        self.assert_integrity_protected_parent(lock_path.parent)
        key = os.path.normcase(str(lock_path))
        with _thread_locks_guard:
            thread_lock = _thread_locks.setdefault(key, threading.RLock())
        with thread_lock:
            held_keys = getattr(_held_file_locks, "keys", None)
            if held_keys is None:
                held_keys = set()
                _held_file_locks.keys = held_keys
            if key in held_keys:
                yield
                return

            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            existing_metadata = None
            if os.path.lexists(lock_path):
                existing_metadata = self._assert_regular_non_redirect(lock_path)
                self._assert_current_owner(lock_path)
            else:
                self._assert_current_owner(lock_path.parent)
            descriptor = os.open(lock_path, flags, 0o600)
            try:
                opened_metadata = os.fstat(descriptor)
                if existing_metadata is not None and not self._same_file(
                    existing_metadata, opened_metadata
                ):
                    raise TokenStoreError(f"{purpose.title()} lock changed: {lock_path}")
                if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_nlink != 1:
                    raise TokenStoreError(
                        f"{purpose.title()} lock is not a single-link regular file: {lock_path}"
                    )
                current_metadata = self._assert_regular_non_redirect(lock_path)
                if not self._same_file(current_metadata, opened_metadata):
                    raise TokenStoreError(f"{purpose.title()} lock changed: {lock_path}")
                if os.name == "nt":
                    self._assert_current_owner(lock_path)
                    self._harden_path(lock_path, directory=False)
                else:
                    if opened_metadata.st_uid != os.geteuid():
                        raise TokenStoreError(
                            f"{purpose.title()} lock is owned by another user: {lock_path}"
                        )
                    os.fchmod(descriptor, 0o600)
                    hardened_metadata = os.fstat(descriptor)
                    if (
                        hardened_metadata.st_uid != os.geteuid()
                        or stat.S_IMODE(hardened_metadata.st_mode) != 0o600
                    ):
                        raise TokenStoreError(f"Could not harden {purpose} lock: {lock_path}")
                if opened_metadata.st_size == 0:
                    os.write(descriptor, b"\0")
                os.fsync(descriptor)
                self._lock_descriptor(descriptor, purpose)
                held_keys.add(key)
                try:
                    yield
                finally:
                    held_keys.discard(key)
                    self._unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int, purpose: str) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    return
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TokenStoreError(f"Timed out waiting for the {purpose} lock") from exc
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _reject_unsafe_target(self) -> None:
        self._assert_safe_path_chain(self.directory)
        if os.path.lexists(self.token_file):
            self._assert_regular_non_redirect(self.token_file)

    def _assert_dedicated_contents(self, *, allow_temporary_tokens: bool = False) -> None:
        """Refuse to claim a directory that contains unrelated user data."""
        if not self.directory.is_dir():
            return
        try:
            with os.scandir(self.directory) as directory_entries:
                entry_names = tuple(entry.name for entry in directory_entries)
        except OSError as exc:
            raise TokenStoreError(
                f"Could not inspect token-store directory: {self.directory}"
            ) from exc
        for entry_name in entry_names:
            if entry_name in DEDICATED_STORE_ENTRIES:
                continue
            if entry_name.startswith(".token-") and entry_name.endswith(".tmp"):
                temporary_path = self.directory / entry_name
                self._assert_owned_temporary_token(temporary_path)
                if allow_temporary_tokens:
                    continue
                raise TokenStoreError(
                    f"A stale temporary token requires writer recovery: {temporary_path}"
                )
            raise TokenStoreError(
                "GARMINTOKENS directory contains unrelated entries and cannot be claimed "
                f"as a dedicated token store; unexpected entry: {self.directory / entry_name}"
            )

    def _discard_interrupted_writes(self, *, keep: Path) -> bool:
        """Discard older artifacts only after another candidate is durable."""
        discarded = False
        for temporary_path in self._temporary_token_paths():
            if temporary_path == keep:
                continue
            self._assert_owned_temporary_token(temporary_path)
            temporary_path.unlink()
            discarded = True
            if os.path.lexists(temporary_path):
                raise TokenStoreError(
                    f"Could not remove interrupted temporary token: {temporary_path}"
                )
        return discarded

    def _temporary_token_paths(self) -> tuple[Path, ...]:
        """Return validated reserved temp paths without reading or deleting them."""
        try:
            with os.scandir(self.directory) as directory_entries:
                temporary_paths = tuple(
                    self.directory / entry.name
                    for entry in directory_entries
                    if entry.name.startswith(".token-") and entry.name.endswith(".tmp")
                )
        except OSError as exc:
            raise TokenStoreError(
                f"Could not inspect token-store directory: {self.directory}"
            ) from exc
        for temporary_path in temporary_paths:
            self._assert_owned_temporary_token(temporary_path)
        return temporary_paths

    def _has_temporary_tokens(self) -> bool:
        """Return whether the store contains a reserved token-temp name."""
        try:
            with os.scandir(self.directory) as directory_entries:
                return any(
                    entry.name.startswith(".token-") and entry.name.endswith(".tmp")
                    for entry in directory_entries
                )
        except OSError as exc:
            raise TokenStoreError(
                f"Could not inspect token-store directory: {self.directory}"
            ) from exc

    @classmethod
    def _assert_owned_temporary_token(cls, path: Path) -> None:
        """Require every secret-bearing interrupted artifact to remain owner-only."""
        metadata = cls._assert_regular_non_redirect(path)
        if os.name == "nt":
            from .windows_acl import acl_is_owner_only, current_user_sid, read_acl_descriptor

            descriptor = read_acl_descriptor(path)
            if descriptor.owner_sid != current_user_sid():
                raise TokenStoreError(f"Temporary token is owned by another SID: {path}")
            if not acl_is_owner_only(path):
                raise TokenStoreError(
                    f"Interrupted token artifact may have exposed secrets; ACL is not "
                    f"owner-only: {path}"
                )
        elif metadata.st_uid != os.geteuid():
            raise TokenStoreError(f"Temporary token is owned by another user: {path}")
        elif stat.S_IMODE(metadata.st_mode) != 0o600:
            raise TokenStoreError(
                f"Interrupted token artifact may have exposed secrets; mode is not 0600: {path}"
            )

    def _directory_permissions_secure(self) -> bool:
        """Check directory ownership/protection without requiring a token file."""
        try:
            self._assert_safe_path_chain(self.directory)
            metadata = os.lstat(self.directory)
            if self._is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
                return False
            if os.name == "nt":
                from .windows_acl import acl_is_owner_only

                return acl_is_owner_only(self.directory)
            return bool(metadata.st_uid == os.geteuid() and stat.S_IMODE(metadata.st_mode) == 0o700)
        except (OSError, TokenStoreError):
            return False

    @classmethod
    def _harden_path(cls, path: Path, *, directory: bool) -> None:
        cls._assert_current_owner(path)
        if os.name == "nt":
            from .windows_acl import WindowsAclError, enforce_owner_only_acl

            try:
                enforce_owner_only_acl(path, directory=directory)
            except WindowsAclError as exc:
                raise TokenStoreError(str(exc)) from exc
            return
        metadata = os.lstat(path)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if cls._is_redirect(metadata) or not expected_type(metadata.st_mode):
            raise TokenStoreError(f"Refusing to harden an unsafe path: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not cls._same_file(metadata, opened_metadata):
                raise TokenStoreError(f"Path changed before permission hardening: {path}")
            if opened_metadata.st_uid != os.geteuid():
                raise TokenStoreError(f"Authentication artifact is foreign-owned: {path}")
            expected_mode = 0o700 if directory else 0o600
            os.fchmod(descriptor, expected_mode)
            hardened_metadata = os.fstat(descriptor)
            if (
                hardened_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(hardened_metadata.st_mode) != expected_mode
                or not cls._same_file(opened_metadata, hardened_metadata)
            ):
                raise TokenStoreError(f"Could not verify permission hardening: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _assert_current_owner(cls, path: Path) -> None:
        """Reject foreign ownership before any chmod, write, or lock creation."""
        metadata = os.lstat(path)
        if cls._is_redirect(metadata):
            raise TokenStoreError(f"Redirected paths are forbidden for token storage: {path}")
        if os.name == "nt":
            from .windows_acl import current_user_sid, read_acl_descriptor

            if read_acl_descriptor(path).owner_sid != current_user_sid():
                raise TokenStoreError(f"Authentication artifact is owned by another SID: {path}")
            return
        if metadata.st_uid != os.geteuid():
            raise TokenStoreError(f"Authentication artifact is owned by another user: {path}")

    @classmethod
    def _assert_safe_path_chain(cls, path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        for part in parts:
            current /= part
            if not os.path.lexists(current):
                continue
            metadata = os.lstat(current)
            if cls._is_redirect(metadata):
                raise TokenStoreError(
                    f"Redirected paths are forbidden for token storage: {current}"
                )

    @classmethod
    def assert_integrity_protected_parent(cls, path: Path) -> None:
        """Require a parent chain whose entries cannot be replaced by another principal."""
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        parts = absolute.parts[1:] if absolute.anchor else absolute.parts
        final_metadata: os.stat_result | None = None
        for part in parts:
            current /= part
            metadata = os.lstat(current)
            if cls._is_redirect(metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise TokenStoreError(f"Path integrity boundary is unsafe: {current}")
            if os.name == "nt":
                from .windows_acl import directory_entry_integrity_is_protected

                if not directory_entry_integrity_is_protected(
                    current,
                    require_current_owner=False,
                ):
                    raise TokenStoreError(
                        "Windows path integrity boundary grants untrusted mutation rights: "
                        f"{current}"
                    )
            else:
                trusted_owners = {0, os.geteuid()}
                if metadata.st_uid not in trusted_owners:
                    raise TokenStoreError(
                        f"Path integrity boundary is owned by an untrusted user: {current}"
                    )
                mode = stat.S_IMODE(metadata.st_mode)
                if mode & 0o022 and not mode & stat.S_ISVTX:
                    raise TokenStoreError(
                        f"Path integrity boundary is writable without sticky protection: {current}"
                    )
            final_metadata = metadata
        if final_metadata is None:
            final_metadata = os.lstat(absolute)
        if os.name != "nt" and final_metadata.st_uid != os.geteuid():
            raise TokenStoreError(
                f"Immediate auth-artifact parent is not owned by the current user: {absolute}"
            )

    @classmethod
    def _assert_regular_non_redirect(cls, path: Path) -> os.stat_result:
        if not os.path.lexists(path):
            raise TokenStoreError(f"Token file is missing: {path}")
        metadata = os.lstat(path)
        if (
            cls._is_redirect(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise TokenStoreError(
                f"Token path is not a single-link regular non-redirect file: {path}"
            )
        return metadata

    @staticmethod
    def _is_redirect(metadata: os.stat_result) -> bool:
        if stat.S_ISLNK(metadata.st_mode):
            return True
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)

    @staticmethod
    def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
        return first.st_dev == second.st_dev and first.st_ino == second.st_ino

    @staticmethod
    def _read_limited(descriptor: int, path: Path) -> bytes:
        size = os.fstat(descriptor).st_size
        if size > MAX_TOKEN_BYTES:
            raise TokenStoreError(f"Token artifact exceeds {MAX_TOKEN_BYTES} bytes: {path}")
        chunks: list[bytes] = []
        remaining = MAX_TOKEN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > MAX_TOKEN_BYTES:
            raise TokenStoreError(f"Token artifact exceeds {MAX_TOKEN_BYTES} bytes: {path}")
        return payload

    def _sync_directory(self) -> None:
        """Durably record the atomic rename where directory fsync is supported."""
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @classmethod
    def _sync_parent_directory(cls, path: Path) -> None:
        """Durably establish a newly created child directory on POSIX."""
        if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
            return
        cls.assert_integrity_protected_parent(path)
        metadata = os.lstat(path)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened_metadata = os.fstat(descriptor)
            if not cls._same_file(metadata, opened_metadata):
                raise TokenStoreError(f"Parent directory changed before durability sync: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
