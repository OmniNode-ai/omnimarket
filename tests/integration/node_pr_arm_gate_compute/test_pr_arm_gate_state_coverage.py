# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for
node_pr_arm_gate_compute (OMN-14151).

Pins this pure COMPUTE node's contract-declared topics and the ARM/WITHHOLD
verdict enum to their literal declared values, mirroring the
node_pr_lifecycle_triage_compute precedent. A silent contract rename or a
dropped decision class now fails here instead of only surfacing at a live
runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_decision import (
    EnumArmDecision,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_arm_gate_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_arm_gate_declares_output_topic() -> None:
    """The contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.pr-arm-gate-decided.v1" in publish_topics


def test_arm_gate_subscribes_merge_candidate_ready_topic() -> None:
    """The contract-declared subscribe topic keeps its literal wire string."""
    subscribe_topics = _load_contract()["event_bus"]["subscribe_topics"]
    assert (
        "onex.evt.omnimarket.pr-lifecycle-merge-candidate-ready.v1" in subscribe_topics
    )


def test_arm_decision_classes_are_pinned() -> None:
    """Every declared ARM/WITHHOLD verdict keeps its literal wire value —
    the orchestrator's merge fanout keys off these exact strings."""
    values = {member.value for member in EnumArmDecision}
    assert values == {"arm", "withhold"}


def test_arm_action_mode_classes_are_pinned() -> None:
    """Every declared action-mode value keeps its literal wire value."""
    values = {member.value for member in EnumArmActionMode}
    assert values == {"report_only", "enforce"}
