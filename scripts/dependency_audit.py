"""Audit the locked environment with validated, time-bounded exceptions."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXCEPTIONS_FILE = ROOT / "audit-exceptions.toml"
REQUIRED_FIELDS = {
    "id",
    "package",
    "reachability",
    "owner",
    "expires",
    "removal_condition",
}


@dataclass(frozen=True, slots=True)
class AuditException:
    """A reviewed temporary exception for one advisory."""

    advisory_id: str
    package: str
    reachability: str
    owner: str
    expires: date
    removal_condition: str


def _required_text(entry: dict[str, Any], index: int, field: str) -> str:
    value = entry[field]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Exception {index} field {field!r} must be a non-empty string")
    return value.strip()


def load_exceptions(
    path: Path = EXCEPTIONS_FILE, *, today: date | None = None
) -> tuple[AuditException, ...]:
    """Load and validate the dependency-audit exception registry."""
    with path.open("rb") as exceptions_file:
        data = tomllib.load(exceptions_file)

    entries = data.get("exceptions", [])
    if not isinstance(entries, list):
        raise ValueError("The 'exceptions' value must be an array of tables")

    current_date = today or date.today()
    seen_ids: set[str] = set()
    exceptions: list[AuditException] = []

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Exception {index} must be a table")

        fields = set(entry)
        missing = REQUIRED_FIELDS - fields
        unknown = fields - REQUIRED_FIELDS
        if missing:
            raise ValueError(f"Exception {index} is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"Exception {index} has unknown fields: {', '.join(sorted(unknown))}")

        advisory_id = _required_text(entry, index, "id")
        normalized_id = advisory_id.casefold()
        if normalized_id in seen_ids:
            raise ValueError(f"Exception {index} duplicates advisory {advisory_id}")
        seen_ids.add(normalized_id)

        expires = entry["expires"]
        if type(expires) is not date:
            raise ValueError(f"Exception {index} field 'expires' must be a TOML date")
        if expires <= current_date:
            raise ValueError(
                f"Exception {index} for {advisory_id} expired on {expires.isoformat()}"
            )

        exceptions.append(
            AuditException(
                advisory_id=advisory_id,
                package=_required_text(entry, index, "package"),
                reachability=_required_text(entry, index, "reachability"),
                owner=_required_text(entry, index, "owner"),
                expires=expires,
                removal_condition=_required_text(entry, index, "removal_condition"),
            )
        )

    return tuple(exceptions)


def main() -> int:
    """Run pip-audit after validating every configured exception."""
    try:
        exceptions = load_exceptions()
    except (OSError, tomllib.TOMLDecodeError, ValueError) as error:
        print(f"Dependency audit configuration failed: {error}", file=sys.stderr)
        return 2

    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--local",
        "--skip-editable",
        "--progress-spinner",
        "off",
    ]
    for exception in exceptions:
        print(
            f"Applying temporary exception {exception.advisory_id} for {exception.package} "
            f"(owner {exception.owner}, expires {exception.expires.isoformat()})",
            flush=True,
        )
        command.extend(("--ignore-vuln", exception.advisory_id))

    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
