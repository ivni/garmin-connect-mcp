"""Tests for the runtime authentication configuration boundary."""

from pathlib import Path

import garmin_connect_mcp.auth as auth_module
from garmin_connect_mcp.auth import DEFAULT_TOKEN_STORE, load_config


def test_runtime_config_does_not_load_legacy_dotenv_files(monkeypatch, tmp_path: Path):
    default_env = tmp_path / ".garminconnect.env"
    default_env.write_text(
        f"GARMIN_PASSWORD=must-not-be-loaded\nGARMINTOKENS={tmp_path / 'dotenv-tokens'}\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "GARMIN_EMAIL=user@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(auth_module, "DEFAULT_ENV_FILE", default_env)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GARMINTOKENS", raising=False)
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)

    config = load_config()

    assert config.token_store == DEFAULT_TOKEN_STORE
    assert set(config.model_dump()) == {"garmintokens"}


def test_runtime_config_accepts_process_token_path(monkeypatch, tmp_path: Path):
    token_directory = tmp_path / "runtime-tokens"
    monkeypatch.setenv("GARMINTOKENS", str(token_directory))

    assert load_config().token_store == token_directory
