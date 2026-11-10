# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node is pure and bus-silent; the contract, the entry point and the node class agree."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_compaction_ground_truth_compute.node import (
    NodeCompactionGroundTruthCompute,
)

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_compaction_ground_truth_compute"
)


def test_contract_declares_no_topic_and_is_pure() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["descriptor"]["side_effects"] == []
    assert "terminal_event" not in contract
    assert [h["operation"] for h in contract["handler_routing"]["handlers"]] == [
        "derive_ground_truth",
        "audit_ground_truth",
    ]


def test_node_class_is_a_compute_node() -> None:
    assert issubclass(NodeCompactionGroundTruthCompute, NodeCompute)


def test_entry_point_is_registered() -> None:
    found = {e.name: e.value for e in entry_points(group="onex.nodes")}
    assert (
        found["node_compaction_ground_truth_compute"]
        == "omnimarket.nodes.node_compaction_ground_truth_compute"
    )


def test_handlers_named_by_the_contract_resolve_to_handle_methods() -> None:
    import importlib

    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    for entry in contract["handler_routing"]["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"]).handle)
        for key in ("input_model", "output_model"):
            module_path, _, name = entry[key].rpartition(".")
            assert hasattr(importlib.import_module(module_path), name)
