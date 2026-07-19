"""Repository-wide test isolation fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from garmin_connect_mcp.token_store import TokenStore, TokenStoreError
from garmin_connect_mcp.types import HeartRateData, SleepData, StepsData, StressData

_PRODUCTION_INTEGRITY_CHECK = TokenStore.assert_integrity_protected_parent.__func__


def trust_windows_test_parent(path: Path) -> None:
    """Install the pytest-private Windows boundary in this process, including spawn children."""
    if os.name != "nt":
        return
    trusted_root = Path(os.path.abspath(path))

    def check_test_parent(cls: type[TokenStore], candidate: Path) -> None:
        absolute = Path(os.path.abspath(candidate))
        if absolute != trusted_root and not absolute.is_relative_to(trusted_root):
            _PRODUCTION_INTEGRITY_CHECK(cls, absolute)
            return
        if not absolute.is_dir():
            raise TokenStoreError(f"Test integrity boundary does not exist: {absolute}")
        from garmin_connect_mcp.windows_acl import directory_entry_integrity_is_protected

        if not directory_entry_integrity_is_protected(
            absolute,
            require_current_owner=False,
        ):
            raise TokenStoreError(
                f"Windows test parent grants untrusted mutation rights: {absolute}"
            )

    TokenStore.assert_integrity_protected_parent = classmethod(  # pyright: ignore[reportAttributeAccessIssue]
        check_test_parent
    )


@pytest.fixture(autouse=True)
def isolate_windows_pytest_temp_boundary():
    """Treat pytest's private child as the test root on an intentionally shared Temp ACL.

    Production validates the complete Windows chain. Windows pytest creates every
    ``tmp_path`` below the user's shared Temp directory, whose ACL deliberately grants
    other sandbox SIDs FILE_DELETE_CHILD. Tests validate the immediate private parent;
    dedicated tests exercise the production parser and hostile parent rejection.
    """
    if os.name != "nt":
        return

    temporary_root = Path(tempfile.gettempdir()).absolute()
    trust_windows_test_parent(temporary_root)


@pytest.fixture
def sample_sleep_data() -> SleepData:
    """Sample sleep data matching Garmin API response structure."""
    return {
        "dailySleepDTO": {
            "sleepStartTimestampLocal": 1705276800000,
            "sleepEndTimestampLocal": 1705305600000,
            "sleepTimeSeconds": 28800,
            "deepSleepSeconds": 7200,
            "lightSleepSeconds": 18000,
            "remSleepSeconds": 3600,
            "awakeSleepSeconds": 600,
            "sleepScores": {
                "overall": {"value": 85, "qualifierKey": "GOOD"},
                "quality": {"value": 82, "qualifierKey": "GOOD"},
                "duration": {"value": 88, "qualifierKey": "GOOD"},
                "recovery": {"value": 84, "qualifierKey": "GOOD"},
            },
        },
        "restlessMomentsCount": 12,
        "avgOvernightHrv": 55.3,
        "restingHeartRate": 58,
        "bodyBatteryChange": 42,
    }


@pytest.fixture
def sample_stress_data() -> StressData:
    """Sample stress data matching Garmin API response structure."""
    return {
        "calendarDate": "2024-01-15",
        "startTimestampLocal": "2024-01-15T00:00:00",
        "endTimestampLocal": "2024-01-15T23:59:59",
        "avgStressLevel": 35,
        "maxStressLevel": 72,
        "stressValuesArray": [[1705276800, 30], [1705280400, 35], [1705284000, 40]],
        "bodyBatteryValuesArray": [[1705276800, 75], [1705280400, 70], [1705284000, 65]],
    }


@pytest.fixture
def sample_heart_rate_data() -> HeartRateData:
    """Sample heart rate data (dictionary format)."""
    return {
        "restingHeartRate": 58,
        "averageHeartRate": 75,
        "minHeartRate": 55,
        "maxHeartRate": 145,
        "heartRateValues": [[1705276800, 60], [1705280400, 65], [1705284000, 70]],
    }


@pytest.fixture
def sample_heart_rate_list():
    """Sample heart rate data (list format)."""
    return [
        [1705276800, 60],
        [1705280400, 65],
        [1705284000, 70],
        [1705287600, 72],
        [1705291200, 68],
    ]


@pytest.fixture
def sample_steps_data() -> StepsData:
    """Sample steps data matching Garmin API response structure."""
    return {
        "totalSteps": 10543,
        "dailyStepGoal": 10000,
        "totalDistanceMeters": 7850.5,
        "activeKilocalories": 425.3,
        "stepsArray": [
            {
                "steps": 1200,
                "startGMT": "2024-01-15T08:00:00",
                "endGMT": "2024-01-15T09:00:00",
            },
            {
                "steps": 2340,
                "startGMT": "2024-01-15T09:00:00",
                "endGMT": "2024-01-15T10:00:00",
            },
            {
                "steps": 1800,
                "startGMT": "2024-01-15T10:00:00",
                "endGMT": "2024-01-15T11:00:00",
            },
        ],
    }
