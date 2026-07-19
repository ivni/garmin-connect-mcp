"""Authentication configuration for Garmin Connect.

Runtime configuration intentionally contains only the token-store location.
Garmin account credentials are bootstrap inputs and must never be persisted by
the application.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ENV_FILE = Path.home() / ".garminconnect.env"
LOCAL_ENV_FILE = Path(".env")
DEFAULT_TOKEN_STORE = Path.home() / ".garminconnect"
DEFAULT_LEGACY_TOKEN_FILE = Path.home() / ".garminconnect_base64"


class GarminConfig(BaseSettings):
    """Runtime configuration for token-based Garmin authentication."""

    garmintokens: str = str(DEFAULT_TOKEN_STORE)

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def token_store(self) -> Path:
        """Return the configured token-store directory."""
        return Path(self.garmintokens).expanduser()


def get_env_file_candidates() -> tuple[Path, ...]:
    """Return every known dotenv target, including a missing crash-recovery target."""
    return tuple(dict.fromkeys((DEFAULT_ENV_FILE, Path.cwd() / LOCAL_ENV_FILE)))


def get_env_file_paths() -> tuple[Path, ...]:
    """Return existing environment files that may contain legacy secrets."""
    return tuple(path for path in get_env_file_candidates() if os.path.lexists(path))


def load_config() -> GarminConfig:
    """Load runtime configuration only from the process environment."""
    return GarminConfig()


def get_legacy_token_path() -> Path:
    """Resolve the deprecated secondary token path for migration only.

    Environment variables take precedence, followed by the same dotenv order
    used by runtime configuration. The value is never used for authentication.
    """
    configured_found, configured = _case_insensitive_lookup(
        os.environ,
        "GARMINTOKENS_BASE64",
    )
    if configured_found:
        return Path(configured).expanduser() if configured else DEFAULT_LEGACY_TOKEN_FILE

    for env_path in reversed(get_env_file_paths()):
        if env_path.is_symlink() or not env_path.is_file():
            continue
        value_found, value = _case_insensitive_lookup(
            dotenv_values(env_path),
            "GARMINTOKENS_BASE64",
        )
        if value_found:
            return Path(value).expanduser() if value else DEFAULT_LEGACY_TOKEN_FILE

    return DEFAULT_LEGACY_TOKEN_FILE


def _case_insensitive_lookup(
    values: Mapping[str, str | None],
    expected_key: str,
) -> tuple[bool, str | None]:
    """Resolve the final case-insensitive match, preserving an empty shadow value."""
    expected = expected_key.casefold()
    found = False
    matched: str | None = None
    for key, value in values.items():
        if key.casefold() == expected:
            found = True
            matched = value
    return found, matched
