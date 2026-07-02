# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for
node_pr_lifecycle_inventory_compute.

OMN-13674 (cluster merge_sweep_pr_lifecycle_compute) under the full
declared-state-coverage DoD and the AST-hardened state-coverage gate
(OMN-13816 / OMN-13781). Pins this COMPUTE node's contract-declared output
state — the publish topic the runtime auto-emits — and the org-wide census
``sweep_done`` fail-closed semantics (OMN-13318) that gate the whole sweep.
A silent contract rename or a regression in the fail-closed census now fails
here instead of only surfacing at a live runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelOrgWideOpenPrInventory,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_lifecycle_inventory_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_inventory_declares_output_topic() -> None:
    """The contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.pr-lifecycle-inventory-completed.v1" in publish_topics


def test_inventory_subscribes_start_topic() -> None:
    """The contract-declared subscribe command topic keeps its literal wire string."""
    subscribe_topics = _load_contract()["event_bus"]["subscribe_topics"]
    assert "onex.cmd.omnimarket.pr-lifecycle-inventory-start.v1" in subscribe_topics


def test_census_sweep_done_only_when_zero_open() -> None:
    """sweep_done is True only when zero open PRs remain org-wide."""
    assert ModelOrgWideOpenPrInventory(open_count=0).sweep_done is True
    assert ModelOrgWideOpenPrInventory(open_count=3).sweep_done is False


def test_census_failed_query_fails_closed() -> None:
    """A failed org-wide query is never reported sweep_done (fail-closed)."""
    census = ModelOrgWideOpenPrInventory(open_count=0, query_failed=True)
    assert census.sweep_done is False
