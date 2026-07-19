"""Tests for authentication CLI routing."""

from __future__ import annotations

import sys

import pytest

from garmin_connect_mcp import cli
from garmin_connect_mcp.scripts import setup_auth


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (["auth"], ("login", {})),
        (["auth", "doctor"], ("doctor", {})),
        (
            ["auth", "migrate"],
            (
                "migrate",
                {
                    "assume_yes": False,
                    "include_local_env": False,
                    "allow_custom_legacy": False,
                    "purge_quarantine": False,
                },
            ),
        ),
        (
            [
                "auth",
                "migrate",
                "--purge",
                "--yes",
                "--include-local-env",
                "--allow-custom-legacy",
            ],
            (
                "migrate",
                {
                    "assume_yes": True,
                    "include_local_env": True,
                    "allow_custom_legacy": True,
                    "purge_quarantine": True,
                },
            ),
        ),
    ],
)
def test_auth_command_routing(monkeypatch, arguments, expected):
    calls = []

    def fake_main(action="login", **kwargs):
        calls.append((action, kwargs))
        return 0

    monkeypatch.setattr(setup_auth, "main", fake_main)
    monkeypatch.setattr(sys, "argv", ["garmin-connect-mcp", *arguments])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0
    assert calls == [expected]


def test_unknown_auth_command_is_rejected(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["garmin-connect-mcp", "auth", "unknown"])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 2
    assert "Unknown auth command" in capsys.readouterr().err


def test_auth_help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["garmin-connect-mcp", "auth", "--help"])

    cli.main()

    assert "auth [doctor|migrate" in capsys.readouterr().out
