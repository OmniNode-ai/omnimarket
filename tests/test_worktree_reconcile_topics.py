# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree-reconcile nodes publish exactly the topics their consumers read."""

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"


def _publishes(node: str) -> list[str]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    return list(contract["event_bus"]["publish_topics"])


def test_effect_publishes_decided_then_run_completed() -> None:
    # The publisher flushes its batch on the second topic, so the order matters.
    assert publish_topics() == (
        "onex.evt.omnimarket.worktree-reconcile-decided.v1",
        "onex.evt.omnimarket.worktree-reconcile-run-completed.v1",
    )


def test_projection_consumes_the_effect_run_completed_topic() -> None:
    contract = yaml.safe_load(
        (NODES / "node_projection_worktree_reconcile" / "contract.yaml").read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == [publish_topics()[1]]
    assert _publishes("node_projection_worktree_reconcile") == [
        "onex.evt.omnimarket.projection-worktree-reconcile-applied.v1"
    ]


def test_compute_publishes_its_evaluation() -> None:
    assert _publishes("node_worktree_reconcile_compute") == [
        "onex.evt.omnimarket.worktree-reconcile-evaluated.v1"
    ]
