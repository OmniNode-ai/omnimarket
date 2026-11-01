# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds virtualenv above the releases the CRLF and seed-integrity advisories cover (OMN-21627).

SFTY-20260930-16258 (CVE-2026-102938, CRLF injection through prompt values) covers
virtualenv <=21.7.10 and SFTY-20260930-15304 (CVE-2026-102930, seed download without an
integrity check) covers <=21.7.11. virtualenv is a transitive dependency (pre-commit),
so its floor is held by ``uv.lock``, as urllib3's is (OMN-21630).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR = (21, 7, 12)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


def test_virtualenv_lock_floor_clears_the_crlf_and_seed_integrity_advisories() -> None:
    lock = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    pinned = [p["version"] for p in lock["package"] if p["name"] == "virtualenv"]
    assert pinned, "uv.lock carries no virtualenv entry"
    assert all(_version(v) >= _FLOOR for v in pinned), pinned
