# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The morning orchestrator is registered in the onex.nodes entry points."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"
_NODE = "node_morning_ground_state_orchestrator"


@pytest.mark.unit
def test_morning_orchestrator_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    nodes = pyproject["project"]["entry-points"]["onex.nodes"]
    assert nodes.get(_NODE) == f"omnimarket.nodes.{_NODE}"
