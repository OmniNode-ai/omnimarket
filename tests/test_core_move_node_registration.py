# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23932: the nodes moved out of core into market are registered and importable."""

from __future__ import annotations

import importlib
import tomllib

import pytest
import yaml

from scripts.audit.market_node_runtime_dogfood import _NODES_DIR, _PYPROJECT

pytestmark = pytest.mark.unit


def _entry_points() -> dict[str, str]:
    with _PYPROJECT.open("rb") as f:
        raw = tomllib.load(f)["project"]["entry-points"]["onex.nodes"]
    return {str(name): str(target) for name, target in raw.items()}


@pytest.mark.parametrize(
    "node",
    [
        "node_dev_tag_lineage_effect",
        "node_effect_backends_check_compute",
        "node_effect_backends_load_effect",
    ],
)
def test_core_move_node_is_registered_and_importable(node: str) -> None:
    entry_points = _entry_points()
    target = entry_points.get(node)
    assert target == f"omnimarket.nodes.{node}"

    contract_path = _NODES_DIR / node / "contract.yaml"
    assert contract_path.is_file()
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
    assert contract.get("name") == node

    assert importlib.import_module(target) is not None
