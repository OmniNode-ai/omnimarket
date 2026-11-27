# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds dnspython at or above 2.9.0 (OMN-26710).

Safety reports SFTY-20261027-41384 in dnspython 2.8.0 (affected spec <2.9.0); the package is
transitive and held by uv.lock, so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR: tuple[int, ...] = (2, 9, 0)
_PACKAGE = "dnspython"


def _version(text: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple."""
    return tuple(int(part) for part in text.split(".")[:3])


def _below_floor(lock_text: str) -> list[str]:
    """Return the pinned dnspython versions below the floor, or ['missing']."""
    lock = tomllib.loads(lock_text)
    pinned = [p["version"] for p in lock["package"] if p["name"] == _PACKAGE]
    if not pinned:
        return ["missing"]
    return [v for v in pinned if _version(v) < _FLOOR]


def test_the_lock_clears_the_dnspython_advisory() -> None:
    """The real uv.lock pins dnspython at or above 2.9.0."""
    assert _below_floor(_LOCK.read_text(encoding="utf-8")) == []


def test_a_lock_at_2_8_0_is_caught() -> None:
    """A lock pinning dnspython 2.8.0 is flagged as below the floor."""
    planted = '[[package]]\nname = "dnspython"\nversion = "2.8.0"\n'
    assert _below_floor(planted) == ["2.8.0"]


def test_a_lock_without_dnspython_is_caught() -> None:
    """A lock missing dnspython is flagged as missing."""
    assert _below_floor('[[package]]\nname = "other"\nversion = "1.0.0"\n') == [
        "missing"
    ]
