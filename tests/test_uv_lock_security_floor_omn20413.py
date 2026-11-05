"""uv.lock keeps mako and multidict past the releases the safety scan flags."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_LOCK = Path(__file__).resolve().parents[1] / "uv.lock"

# Safety advisories SFTY-20261005-32026 (mako <=1.4.1) and
# SFTY-20261005-09094 (multidict >=6.7.0,<=6.9.0).
_FIXED_FROM = {"mako": (1, 4, 2), "multidict": (6, 9, 1)}


def _locked_versions() -> dict[str, tuple[int, ...]]:
    data = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    return {
        p["name"]: tuple(int(x) for x in p["version"].split(".")[:3])
        for p in data["package"]
        if p["name"] in _FIXED_FROM
    }


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_FIXED_FROM))
def test_locked_version_is_past_the_flagged_range(name: str) -> None:
    locked = _locked_versions()
    assert name in locked
    assert locked[name] >= _FIXED_FROM[name]
