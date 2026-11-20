# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds certifi at or above the release that fixes SFTY-20261020-58773 (OMN-26116).

The advisory (insufficient verification of data authenticity) covers certifi
>=2019.3.9,<2026.10.18. certifi is a transitive dependency whose floor is held by
``uv.lock``, not by a constraint list (see the security-floor note in
``pyproject.toml``), so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR = (2026, 10, 18)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


def test_certifi_lock_floor_clears_the_authenticity_advisory() -> None:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    pinned = [p["version"] for p in lock["package"] if p["name"] == "certifi"]
    assert pinned, "uv.lock carries no certifi entry"
    assert all(_version(v) >= _FLOOR for v in pinned), pinned
