"""Keep the install closure above the reviewed advisory floors from issue #1023."""

import re
from pathlib import Path
from typing import Final

import pytest
from packaging.version import Version

ROOT: Final = Path(__file__).resolve().parents[3]
ADVISORY_FLOORS: Final = {
    "aiohttp": "3.14.4",
    "anyio": "4.14.2",
    "certifi": "2026.10.18",
    "idna": "3.15",
    "multidict": "6.9.1",
    "python-multipart": "0.0.31",
    "starlette": "1.3.1",
}


@pytest.mark.parametrize(("package", "floor"), ADVISORY_FLOORS.items())
def test_pinned_version_meets_advisory_floor(package: str, floor: str) -> None:
    # Given the exact versions shipped by constraints consumers.
    pins = dict(
        re.findall(
            r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$",
            (ROOT / "constraints.txt").read_text(encoding="utf-8"),
            flags=re.MULTILINE,
        )
    )

    # When comparing a pin with its first release clear of the reviewed advisories.
    pinned = Version(pins[package])

    # Then downgrades below the advisory floor fail independently for each package.
    assert pinned >= Version(floor), (
        f"{package} pinned at {pinned} is below advisory floor {floor} (issue #1023)"
    )
