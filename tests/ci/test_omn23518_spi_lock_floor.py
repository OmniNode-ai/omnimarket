# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill orchestrator resolves omnibase-spi at or above 0.23.15 (OMN-23518).

The floor is held in ``project.dependencies`` and in ``tool.uv.override-dependencies``
of ``pyproject.toml``, once each. ``uv.lock`` does not record those specifiers: for a
package carrying a ``[tool.uv.sources]`` git source it records the git source in
``manifest.overrides`` and in the root ``requires-dist``, and the resolved version in
its package entry. So the lock is tied to the floor through that locked version, and
the installed distribution is tied to the lock through ``importlib.metadata``.
"""

from __future__ import annotations

import importlib.metadata
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.version import Version

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_FLOOR = Version("0.23.15")
_CEILING = Version("0.24")
_SPI_SPEC = "omnibase-spi>=0.23.15,<0.24.0"
_SPI = "omnibase-spi"


def _in_range(version: str) -> bool:
    """True when ``version`` is at or above the floor and below 0.24 (rc/dev safe)."""
    parsed = Version(version)
    return parsed >= _FLOOR and Version(parsed.base_version) < _CEILING


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))


def _lock() -> dict[str, Any]:
    return tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))


def _spi_entries(entries: list[str]) -> list[str]:
    return [e for e in entries if e.replace(" ", "").startswith(_SPI + ">")]


def _locked_spi_versions() -> list[str]:
    return [p["version"] for p in _lock()["package"] if p["name"] == _SPI]


@pytest.mark.parametrize("version", ["0.23.15", "0.23.16", "0.23.16rc1", "0.23.99"])
def test_in_range_accepts_the_floor_and_later_0_23_releases(version: str) -> None:
    assert _in_range(version), version


@pytest.mark.parametrize(
    "version", ["0.23.14", "0.23.3", "0.24.0", "0.24.0rc1", "0.24.0.dev1", "1.0.0"]
)
def test_in_range_rejects_below_the_floor_and_the_0_24_line(version: str) -> None:
    assert not _in_range(version), version


def test_pyproject_floors_agree() -> None:
    doc = _pyproject()
    in_deps = _spi_entries(doc["project"]["dependencies"])
    in_overrides = _spi_entries(doc["tool"]["uv"]["override-dependencies"])
    assert in_deps == [_SPI_SPEC], in_deps
    assert in_overrides == [_SPI_SPEC], in_overrides


def test_lock_resolves_spi_in_range_from_the_declared_source() -> None:
    lock = _lock()
    source = _pyproject()["tool"]["uv"]["sources"][_SPI]
    locked_source = f"{source['git']}?tag={source['tag']}"
    pinned = [p for p in lock["package"] if p["name"] == _SPI]
    assert len(pinned) == 1, pinned
    assert _in_range(pinned[0]["version"]), pinned[0]["version"]
    assert pinned[0]["source"]["git"].startswith(locked_source), pinned[0]["source"]
    overrides = [o for o in lock["manifest"]["overrides"] if o["name"] == _SPI]
    assert [o["git"] for o in overrides] == [locked_source], overrides
    root = next(p for p in lock["package"] if p["name"] == "omnimarket")
    required = [r for r in root["metadata"]["requires-dist"] if r["name"] == _SPI]
    assert [r["git"] for r in required] == [locked_source], required


def test_lane_process_types_import() -> None:
    installed = importlib.metadata.version(_SPI)
    assert [installed] == _locked_spi_versions(), (
        f"installed omnibase-spi {installed} differs from the locked version"
    )
    from omnibase_spi.contracts.services import (
        ModelLaneProcessRunResult,
        ModelLaneProcessStartResult,
        ModelLaneStartSpec,
    )

    types = (ModelLaneProcessRunResult, ModelLaneProcessStartResult, ModelLaneStartSpec)
    assert all(isinstance(t, type) for t in types)
