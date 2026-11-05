# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""uv.lock keeps the locked packages the Dependency Security Scan flagged at their fixed versions."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

LOCK = Path(__file__).resolve().parent.parent / "uv.lock"

# Package -> first fixed version, from the pip-audit and safety reports on dev.
FIXED_FLOORS = {
    "multidict": (6, 9, 1),
    "certifi": (2026, 10, 23),
    "dnspython": (2, 9, 2),
    "filelock": (4, 2, 1),
    "pyasn1": (0, 6, 5),
}


def _locked_versions() -> dict[str, tuple[int, ...]]:
    data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    return {
        p["name"]: tuple(int(x) for x in p["version"].split(".") if x.isdigit())
        for p in data["package"]
        if "version" in p
    }


@pytest.mark.unit
@pytest.mark.parametrize(("name", "floor"), sorted(FIXED_FLOORS.items()))
def test_locked_version_is_at_or_above_fixed_floor(
    name: str, floor: tuple[int, ...]
) -> None:
    assert _locked_versions()[name] >= floor
