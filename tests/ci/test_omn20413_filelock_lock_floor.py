# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds filelock above the releases the Dependency Security Scan flags (OMN-20413).

Safety reports SFTY-20261017-72390, SFTY-20261017-54654 and SFTY-20261017-57659
(CVE-2026-112910, CVE-2026-112908, CVE-2026-112938) against filelock
>=3.10.0,<=4.2.0. filelock is a transitive dependency whose floor is held by
``uv.lock``, so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FIRST_FIXED = (4, 2, 1)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


def test_filelock_lock_floor_clears_the_permission_and_race_advisories() -> None:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    pinned = [p["version"] for p in lock["package"] if p["name"] == "filelock"]
    assert pinned, "uv.lock carries no filelock entry"
    assert all(_version(v) >= _FIRST_FIXED for v in pinned), pinned
