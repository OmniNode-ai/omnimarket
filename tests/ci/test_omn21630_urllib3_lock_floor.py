# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds urllib3 at or above the release that fixes SFTY-20260930-39688 (OMN-21630).

The advisory (CVE-2026-97687, improper certificate validation) covers urllib3
>=1.26.0,<2.8.0. urllib3 is a transitive dependency whose floor is held by
``uv.lock``, not by a constraint list (see the security-floor note in
``pyproject.toml``), so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR = (2, 8, 0)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


def test_urllib3_lock_floor_clears_the_certificate_advisory() -> None:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    pinned = [p["version"] for p in lock["package"] if p["name"] == "urllib3"]
    assert pinned, "uv.lock carries no urllib3 entry"
    assert all(_version(v) >= _FLOOR for v in pinned), pinned
