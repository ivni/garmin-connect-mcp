"""Run the repository quality gate in the uv-managed environment."""

from __future__ import annotations

import subprocess
import sys

CHECKS = (
    ("Ruff lint", (sys.executable, "-m", "ruff", "check")),
    ("Ruff format", (sys.executable, "-m", "ruff", "format", "--check")),
    ("Pyright", (sys.executable, "-m", "pyright")),
    ("Pytest", (sys.executable, "-m", "pytest")),
    (
        "Dependency audit",
        (sys.executable, "scripts/dependency_audit.py"),
    ),
)


def main() -> int:
    """Run each QA check in order and stop at the first failure."""
    for name, command in CHECKS:
        print(f"==> {name}", flush=True)
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            print(f"{name} failed with exit code {result.returncode}", file=sys.stderr)
            return result.returncode

    print("==> QA passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
