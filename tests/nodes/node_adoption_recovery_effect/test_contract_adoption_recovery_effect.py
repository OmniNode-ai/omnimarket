# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged contract of node_adoption_recovery_effect and its onex.nodes registration."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_adoption_recovery_effect"


def load_contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_the_contract_names_the_handler_and_is_not_bus_routed() -> None:
    contract = load_contract()
    assert contract["name"] == "node_adoption_recovery_effect"
    assert contract["lifecycle"] == "experimental"
    assert contract["handler"]["name"] == "HandlerAdoptionRecovery"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        "HandlerAdoptionRecovery",
    )
    assert "handler_routing" not in contract
    assert "event_bus" not in contract


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        entry_points["node_adoption_recovery_effect"]
        == "omnimarket.nodes.node_adoption_recovery_effect"
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_adoption_recovery_effect"
    assert (
        metadata["entry_points"]["onex.nodes"]["node_adoption_recovery_effect"]
        == "omnimarket.nodes.node_adoption_recovery_effect"
    )
    assert (
        importlib.import_module(
            "omnimarket.nodes.node_adoption_recovery_effect"
        ).__name__
        == "omnimarket.nodes.node_adoption_recovery_effect"
    )
