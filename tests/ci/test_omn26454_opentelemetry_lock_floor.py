# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lock holds opentelemetry-api and opentelemetry-exporter-otlp-proto-http at or above 1.45.2 (OMN-26454).

Safety reports SFTY-20261022-15927 in opentelemetry-api 1.44.0 and SFTY-20261018-93041 in
opentelemetry-exporter-otlp-proto-http 1.44.0; both are transitive and held by uv.lock,
so the floor is asserted on the lock itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FLOOR: tuple[int, ...] = (1, 45, 2)
_PACKAGES: tuple[str, ...] = (
    "opentelemetry-api",
    "opentelemetry-exporter-otlp-proto-http",
)


def _version(text: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable integer tuple."""
    return tuple(int(part) for part in text.split(".")[:3])


def _below_floor(lock_text: str) -> dict[str, list[str]]:
    """Return the packages in the lock whose pinned versions are below the floor."""
    lock = tomllib.loads(lock_text)
    result: dict[str, list[str]] = {}
    for name in _PACKAGES:
        pinned = [p["version"] for p in lock["package"] if p["name"] == name]
        if not pinned:
            result[name] = ["missing"]
        else:
            below = [v for v in pinned if _version(v) < _FLOOR]
            if below:
                result[name] = below
    return result


def test_the_lock_clears_both_opentelemetry_advisories() -> None:
    """The real uv.lock pins both packages at or above 1.45.2."""
    assert _below_floor(_LOCK.read_text(encoding="utf-8")) == {}


def test_a_lock_at_1_44_0_is_caught() -> None:
    """A lock pinning both packages at 1.44.0 is flagged as below the floor."""
    planted = (
        '[[package]]\nname = "opentelemetry-api"\nversion = "1.44.0"\n'
        '\n[[package]]\nname = "opentelemetry-exporter-otlp-proto-http"\n'
        'version = "1.44.0"\n'
    )
    assert _below_floor(planted) == {
        "opentelemetry-api": ["1.44.0"],
        "opentelemetry-exporter-otlp-proto-http": ["1.44.0"],
    }


def test_a_lock_without_the_packages_is_caught() -> None:
    """A lock missing both packages is flagged as missing."""
    assert set(_below_floor('[[package]]\nname = "other"\nversion = "1.0.0"\n')) == set(
        _PACKAGES
    )
