# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged contract of node_controller_fix_plan_compute and its onex.nodes registration."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_controller_fix_plan_compute"


def load_contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_the_contract_names_its_handler_command_topic_and_terminal_event() -> None:
    contract = load_contract()
    assert contract["name"] == "node_controller_fix_plan_compute"
    assert contract["handler"]["class"] == "HandlerControllerFixPlan"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        "HandlerControllerFixPlan",
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.controller-fix-plan-requested.v1"
    )
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.controller-fix-plan-completed.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.controller-fix-plan-completed.v1"
    ]
    assert contract["externally_consumed_topics"] == [
        "onex.evt.omnimarket.controller-fix-plan-completed.v1"
    ]
    assert (
        "onex.cmd.omnimarket.controller-fix-plan-requested.v1"
        in contract["event_bus"]["subscribe_topics"]
    )


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        entry_points["node_controller_fix_plan_compute"]
        == "omnimarket.nodes.node_controller_fix_plan_compute"
    )
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_controller_fix_plan_compute"
    assert (
        metadata["entry_points"]["onex.nodes"]["node_controller_fix_plan_compute"]
        == "omnimarket.nodes.node_controller_fix_plan_compute"
    )
    assert (
        importlib.import_module(
            "omnimarket.nodes.node_controller_fix_plan_compute"
        ).__name__
        == "omnimarket.nodes.node_controller_fix_plan_compute"
    )
