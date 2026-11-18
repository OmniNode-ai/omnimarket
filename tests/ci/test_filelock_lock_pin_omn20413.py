"""uv.lock must resolve filelock outside the range the safety advisories name (OMN-20413)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LOCK = Path(__file__).resolve().parents[2] / "uv.lock"
_FIXED = (4, 2, 1)


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3])


def test_uv_lock_resolves_filelock_at_or_above_fixed_version() -> None:
    data = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    versions = [p["version"] for p in data["package"] if p["name"] == "filelock"]
    assert len(versions) == 1, versions
    assert _version_tuple(versions[0]) >= _FIXED, (
        f"uv.lock pins filelock {versions[0]}, below the fixed {_FIXED}"
    )
