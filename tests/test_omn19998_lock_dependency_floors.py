"""uv.lock must not pin Mako or multidict inside the advisory ranges."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

LOCK = Path(__file__).resolve().parents[1] / "uv.lock"
# Versions must be strictly above these floors (SFTY-20261005-32026, SFTY-20261005-09094).
FLOORS = {"mako": (1, 4, 1), "multidict": (6, 9, 0)}


def _packages() -> list[dict[str, object]]:
    return tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]


@pytest.mark.parametrize("name", sorted(FLOORS))
def test_lock_pins_package_above_advisory_floor(name: str) -> None:
    entries = [p for p in _packages() if p["name"] == name]
    assert entries, f"{name} has no uv.lock entry"
    for entry in entries:
        version = tuple(int(x) for x in str(entry["version"]).split("."))
        assert version > FLOORS[name], (
            f"{name} {entry['version']} is in the advisory range"
        )
