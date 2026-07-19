from __future__ import annotations

from datetime import date

import pytest

from scripts.dependency_audit import load_exceptions


def test_load_exceptions_accepts_an_empty_registry(tmp_path):
    registry = tmp_path / "audit-exceptions.toml"
    registry.write_text("# No exceptions\n", encoding="utf-8")

    assert load_exceptions(registry, today=date(2026, 7, 19)) == ()


def test_load_exceptions_accepts_complete_future_exception(tmp_path):
    registry = tmp_path / "audit-exceptions.toml"
    registry.write_text(
        """
[[exceptions]]
id = "GHSA-xxxx-xxxx-xxxx"
package = "example-package"
reachability = "The vulnerable feature is not used."
owner = "@maintainer"
expires = 2026-08-19
removal_condition = "Remove when the compatible fixed release is available."
""".strip(),
        encoding="utf-8",
    )

    exceptions = load_exceptions(registry, today=date(2026, 7, 19))

    assert len(exceptions) == 1
    assert exceptions[0].advisory_id == "GHSA-xxxx-xxxx-xxxx"
    assert exceptions[0].expires == date(2026, 8, 19)


@pytest.mark.parametrize(
    ("registry_text", "error"),
    [
        (
            """
[[exceptions]]
id = "GHSA-xxxx-xxxx-xxxx"
package = "example-package"
owner = "@maintainer"
expires = 2026-08-19
removal_condition = "Upgrade when possible."
""",
            "missing fields: reachability",
        ),
        (
            """
[[exceptions]]
id = "GHSA-xxxx-xxxx-xxxx"
package = "example-package"
reachability = "Not reachable."
owner = "@maintainer"
expires = 2026-07-19
removal_condition = "Upgrade when possible."
""",
            "expired on 2026-07-19",
        ),
        (
            """
[[exceptions]]
id = "GHSA-xxxx-xxxx-xxxx"
package = "example-package"
reachability = "Not reachable."
owner = "@maintainer"
expires = 2026-08-19
removal_condition = "Upgrade when possible."

[[exceptions]]
id = "ghsa-xxxx-xxxx-xxxx"
package = "example-package"
reachability = "Not reachable."
owner = "@maintainer"
expires = 2026-08-20
removal_condition = "Upgrade when possible."
""",
            "duplicates advisory ghsa-xxxx-xxxx-xxxx",
        ),
    ],
)
def test_load_exceptions_rejects_invalid_registry(tmp_path, registry_text, error):
    registry = tmp_path / "audit-exceptions.toml"
    registry.write_text(registry_text.strip(), encoding="utf-8")

    with pytest.raises(ValueError, match=error):
        load_exceptions(registry, today=date(2026, 7, 19))
