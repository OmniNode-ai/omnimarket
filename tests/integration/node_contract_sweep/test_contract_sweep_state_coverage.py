# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression tests for node_contract_sweep.

OMN-13674 / OMN-13675 (WS-5 Wave 1) under the strengthened full
declared-state-coverage DoD and the AST-hardened state-coverage gate
(OMN-13816). Pins this node's contract-declared output states — the
publish topics the runtime auto-emits and the output-class fields the
projection consumes — to their literal declared values. A silent
contract rename or removal of any declared state now fails here instead
of only surfacing at a live runtime/projection boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_contract_sweep"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_contract_sweep_declares_output_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.contract-sweep-completed.v1" in publish_topics
    assert "onex.evt.omnimarket.contract-sweep-violation.v1" in publish_topics
