# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""uv.lock holds the fixed versions of two packages the safety gate flagged."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

LOCK = Path(__file__).resolve().parents[1] / "uv.lock"

# package -> first version past the advisory's affected range
# multidict SFTY-20261005-09094 affects >=6.7.0,<=6.9.0
# mako SFTY-20261005-32026 affects <=1.4.1
FLOORS = {"multidict": (6, 9, 1), "mako": (1, 4, 2)}


def _locked_version(name: str) -> tuple[int, ...]:
    data = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    versions = [p["version"] for p in data["package"] if p["name"] == name]
    assert versions, f"{name} is not in uv.lock"
    return tuple(int(x) for x in versions[0].split(".")[:3])


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(FLOORS))
def test_locked_version_is_past_the_advisory(name: str) -> None:
    assert _locked_version(name) >= FLOORS[name]
