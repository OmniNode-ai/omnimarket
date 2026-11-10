# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill orchestrator resolves omnibase-spi at or above 0.23.15 (OMN-23518).
The floor is held in both dependency lists of ``pyproject.toml`` and in ``uv.lock``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_FLOOR = (0, 23, 15)
_SPI_SPEC = "omnibase-spi>=0.23.15,<0.24.0"
_DEP_KEYS = ("dependencies", "override-dependencies")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(".")[:3])


def _spi_holds_spi(value: object) -> bool:
    return isinstance(value, list) and any(
        isinstance(entry, str) and entry.startswith("omnibase-spi") for entry in value
    )


def _spi_lists(doc: object) -> list[list[str]]:
    found: list[list[str]] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key in _DEP_KEYS and _spi_holds_spi(value):
                found.append(value)
            found.extend(_spi_lists(value))
    elif isinstance(doc, list):
        for item in doc:
            found.extend(_spi_lists(item))
    return found


def test_lock_resolves_spi_at_or_above_the_lane_process_floor() -> None:
    lock = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    pinned = [p["version"] for p in lock["package"] if p["name"] == "omnibase-spi"]
    assert pinned, "uv.lock carries no omnibase-spi entry"
    assert all(_version(v) >= _FLOOR for v in pinned), pinned


def test_pyproject_floors_agree() -> None:
    doc = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    lists = _spi_lists(doc)
    assert len(lists) >= 2, lists
    for entries in lists:
        spi = [entry for entry in entries if entry.startswith("omnibase-spi")]
        assert all(entry == _SPI_SPEC for entry in spi), spi


def test_lane_process_types_import() -> None:
    from omnibase_spi.contracts.services import (
        ModelLaneProcessRunResult,
        ModelLaneProcessStartResult,
        ModelLaneStartSpec,
    )

    types = (ModelLaneProcessRunResult, ModelLaneProcessStartResult, ModelLaneStartSpec)
    assert all(isinstance(t, type) for t in types)
