# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds grpcio at or above 1.84.1 (OMN-26115).

Safety reports SFTY-20261027-51483 for grpcio >=1.84.0,<1.84.1. grpcio is
transitive and held by uv.lock, so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR: tuple[int, ...] = (1, 84, 1)


def _version(text: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple."""
    return tuple(int(part) for part in text.split(".")[:3])


def _below_floor(lock_text: str) -> list[str]:
    """Return the grpcio versions in the lock below the floor, or ['missing']."""
    lock = tomllib.loads(lock_text)
    pinned = [p["version"] for p in lock["package"] if p["name"] == "grpcio"]
    if not pinned:
        return ["missing"]
    return [v for v in pinned if _version(v) < _FLOOR]


def test_the_lock_clears_the_grpcio_advisory() -> None:
    """The real uv.lock pins grpcio at or above 1.84.1."""
    assert _below_floor(_LOCK.read_text(encoding="utf-8")) == []


def test_a_lock_at_1_84_0_is_caught() -> None:
    """A lock pinning grpcio 1.84.0 is flagged as below the floor."""
    planted = '[[package]]\nname = "grpcio"\nversion = "1.84.0"\n'
    assert _below_floor(planted) == ["1.84.0"]


def test_a_lock_without_grpcio_is_caught() -> None:
    """A lock missing grpcio is flagged as missing."""
    assert _below_floor('[[package]]\nname = "other"\nversion = "1.0.0"\n') == [
        "missing"
    ]
