# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract sanity for node_pr_lifecycle_worktree_prune_effect (OMN-13859)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_worktree_prune_effect"
    / "contract.yaml"
)


@pytest.mark.unit
def test_contract_is_an_effect_node() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert data["name"] == "pr_lifecycle_worktree_prune_effect"
    assert data["node_type"] == "effect"
    assert data["descriptor"]["node_archetype"] == "effect"


@pytest.mark.unit
def test_contract_declares_prune_topics() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert (
        "onex.cmd.omnimarket.pr-lifecycle-worktree-prune-start.v1"
        in data["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.pr-lifecycle-worktree-prune-completed.v1"
        in data["event_bus"]["publish_topics"]
    )


@pytest.mark.unit
def test_contract_handler_points_at_real_handler() -> None:
    data = yaml.safe_load(_CONTRACT.read_text())
    assert data["handler"]["class"] == "HandlerWorktreePrune"
    assert (
        data["handler"]["module"]
        == "omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune"
    )
