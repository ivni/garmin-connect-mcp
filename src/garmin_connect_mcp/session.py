"""Application-level Garmin session and token-generation lifecycle."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from .auth import GarminConfig, load_config
from .client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminClientWrapper,
    GarminMutationClient,
    GarminRateLimitError,
    GarminReadClient,
    MutationOperation,
    MutationRegistry,
)
from .token_store import TokenStore, TokenStoreConflict, TokenStoreError
from .write_policy import READ_METHODS

GarminFactory = Callable[..., Garmin]
ConfigLoader = Callable[[], GarminConfig]
_INLINE_TOKEN_THRESHOLD = 512


class GarminSessionManager:
    """Own one in-memory client bound to a canonical token generation."""

    def __init__(
        self,
        config_loader: ConfigLoader = load_config,
        garmin_factory: GarminFactory = Garmin,
    ):
        self._config_loader = config_loader
        self._garmin_factory = garmin_factory
        self._lock = threading.RLock()
        self._client: GarminClientWrapper | None = None
        self._garmin: Garmin | None = None
        self._generation: str | None = None
        self._mutation_registries: dict[str, MutationRegistry] = {}

    def get_read_client(self) -> GarminReadClient:
        """Return a facade that cannot invoke Garmin mutation methods."""
        return GarminReadClient(self._get_client(), READ_METHODS)

    def get_mutation_client(
        self,
        operation: MutationOperation,
    ) -> GarminMutationClient:
        """Return a facade restricted to one authorized mutation method."""
        with self._lock:
            client = self._get_client()
            store = self.get_token_store()
            key = str(store.directory)
            registry = self._mutation_registries.get(key)
            if registry is None:
                registry = MutationRegistry(journal=store)
                self._mutation_registries[key] = registry
            return GarminMutationClient(client, operation, registry)

    def _get_client(self) -> GarminClientWrapper:
        """Load the unrestricted client for construction of restricted facades only."""
        with self._lock:
            store = self.get_token_store()
            try:
                snapshot = store.read_snapshot()
            except TokenStoreError as exc:
                self._invalidate()
                raise GarminAuthenticationError(
                    "No secure, valid Garmin token store was found. "
                    "Run 'garmin-connect-mcp auth doctor' and then authenticate or migrate.",
                    original_error=exc,
                ) from exc

            if self._client is not None and self._generation == snapshot.fingerprint:
                return self._client
            self._invalidate()

            # A concurrent auth command can replace the generation while login
            # verifies the profile. Retry once from the winner instead of ever
            # allowing the stale in-memory state to overwrite it.
            for _attempt in range(2):
                try:
                    garmin = self._garmin_factory()
                    garmin.login(_inline_token_payload(snapshot.payload))
                    serialized = garmin.client.dumps()
                    # Even unchanged serialization must prove that the loaded
                    # generation is still canonical before this client is published.
                    snapshot = store.compare_and_replace(
                        serialized,
                        expected_fingerprint=snapshot.fingerprint,
                    )
                except TokenStoreConflict:
                    snapshot = store.read_snapshot()
                    continue
                except GarminConnectAuthenticationError as exc:
                    raise GarminAuthenticationError(original_error=exc) from exc
                except GarminConnectTooManyRequestsError as exc:
                    raise GarminRateLimitError(original_error=exc) from exc
                except (GarminConnectConnectionError, OSError, TokenStoreError) as exc:
                    raise GarminAPIError(
                        f"Failed to load the Garmin token store: {exc}",
                        original_error=exc,
                    ) from exc

                self._garmin = garmin
                self._generation = snapshot.fingerprint
                self._client = GarminClientWrapper(
                    garmin,
                    lock=self._lock,
                    before_call=self._validate_current_generation,
                    after_call=self._persist_current_generation,
                    on_authentication_error=self.reset,
                )
                return self._client

            raise GarminAuthenticationError(
                "The Garmin token changed repeatedly during login. Retry the request."
            )

    def authenticate(
        self,
        email: str,
        password: str,
        prompt_mfa: Callable[[], str],
    ) -> TokenStore:
        """Authenticate with ephemeral credentials and atomically store tokens."""
        store = self.get_token_store()
        with self._lock:
            try:
                garmin = self._garmin_factory(
                    email=email,
                    password=password,
                    prompt_mfa=prompt_mfa,
                )
                # garminconnect otherwise consults GARMINTOKENS from the process
                # environment and can silently reuse the old canonical file. An
                # invalid inline blob forces credential login without any disk writer.
                garmin.login(_invalid_inline_token_payload())
                store.replace_payload(garmin.client.dumps())
            except GarminConnectAuthenticationError as exc:
                raise GarminAuthenticationError(original_error=exc) from exc
            except GarminConnectTooManyRequestsError as exc:
                raise GarminRateLimitError(original_error=exc) from exc
            except (GarminConnectConnectionError, OSError, TokenStoreError) as exc:
                raise GarminAPIError(
                    f"Failed to authenticate or store Garmin tokens: {exc}",
                    original_error=exc,
                ) from exc

            self._invalidate()
        return store

    def get_token_store(self) -> TokenStore:
        """Resolve the canonical store from current runtime configuration."""
        return TokenStore(self._config_loader().garmintokens)

    def reset(self) -> None:
        """Discard the cached client without mutating persisted tokens."""
        with self._lock:
            self._invalidate()

    def _persist_current_generation(self) -> None:
        if self._garmin is None or self._generation is None:
            raise GarminAuthenticationError("The Garmin session is no longer current.")
        try:
            payload = self._garmin.client.dumps()
            committed = self.get_token_store().compare_and_replace(
                payload,
                expected_fingerprint=self._generation,
            )
            self._generation = committed.fingerprint
        except TokenStoreConflict as exc:
            self._invalidate()
            raise GarminAuthenticationError(
                "Garmin authentication changed in another process. Retry the request.",
                original_error=exc,
            ) from exc
        except (OSError, TokenStoreError) as exc:
            self._invalidate()
            raise GarminAPIError(
                f"Failed to persist refreshed Garmin tokens: {exc}",
                original_error=exc,
            ) from exc
        except Exception as exc:
            self._invalidate()
            raise GarminAPIError(
                f"Unexpected Garmin token persistence failure: {exc}",
                original_error=exc,
            ) from exc

    def _validate_current_generation(self) -> None:
        """Refuse network use after another process replaces authentication."""
        if self._client is None or self._garmin is None or self._generation is None:
            raise GarminAuthenticationError("The Garmin session is no longer current.")
        try:
            current = self.get_token_store().read_snapshot()
        except (OSError, TokenStoreError) as exc:
            self._invalidate()
            raise GarminAuthenticationError(
                "The canonical Garmin authentication generation is unavailable. Retry.",
                original_error=exc,
            ) from exc
        if current.fingerprint != self._generation:
            conflict = TokenStoreConflict("The canonical Garmin token changed in another process.")
            self._invalidate()
            raise GarminAuthenticationError(
                "Garmin authentication changed in another process. Retry the request.",
                original_error=conflict,
            ) from conflict

    def _invalidate(self) -> None:
        if self._client is not None:
            self._client.revoke()
        self._client = None
        self._garmin = None
        self._generation = None


def _inline_token_payload(payload: str) -> str:
    """Select garminconnect's in-memory token branch without a filesystem path."""
    TokenStore.validate_payload(payload)
    minimum_length = _INLINE_TOKEN_THRESHOLD + 1
    return payload if len(payload) >= minimum_length else payload.ljust(minimum_length)


def _invalid_inline_token_payload() -> str:
    """Force credential login while preventing garminconnect disk persistence."""
    payload = json.dumps({"garmin_connect_mcp_bootstrap": None})
    return payload.ljust(_INLINE_TOKEN_THRESHOLD + 1)


_session_manager: GarminSessionManager | None = None
_session_manager_lock = threading.Lock()


def get_session_manager() -> GarminSessionManager:
    """Return the process-wide Garmin session manager."""
    global _session_manager
    with _session_manager_lock:
        if _session_manager is None:
            _session_manager = GarminSessionManager()
        return _session_manager


def set_session_manager(manager: GarminSessionManager | None) -> None:
    """Replace the process-wide manager for tests or application composition."""
    global _session_manager
    with _session_manager_lock:
        _session_manager = manager
