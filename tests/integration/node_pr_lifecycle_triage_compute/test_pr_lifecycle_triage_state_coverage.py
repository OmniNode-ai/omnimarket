# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for
node_pr_lifecycle_triage_compute.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute) under the full
declared-state-coverage DoD and the AST-hardened state-coverage gate
(OMN-13816 / OMN-13781). Pins this COMPUTE node's contract-declared output
state — the publish topic the runtime auto-emits — and its verdict enum to
their literal declared values. A silent contract rename or a dropped verdict
class now fails here instead of only surfacing at a live runtime/projection
boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_triage_category import (
    EnumPrTriageCategory,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_triage_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_triage_declares_output_topic() -> None:
    """The contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.pr-lifecycle-triage-completed.v1" in publish_topics


def test_triage_subscribes_inventory_completed_topic() -> None:
    """The contract-declared subscribe topic keeps its literal wire string."""
    subscribe_topics = _load_contract()["event_bus"]["subscribe_topics"]
    assert "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1" in subscribe_topics


def test_triage_verdict_classes_are_pinned() -> None:
    """Every declared triage verdict class keeps its literal wire value.

    The projection and downstream reducer key off these exact strings; a
    rename here is a contract break, so pin the full set literally.
    """
    values = {member.value for member in EnumPrTriageCategory}
    assert values == {
        "green",
        "red",
        "conflicted",
        "occ_dependency",
        "needs_review",
    }
