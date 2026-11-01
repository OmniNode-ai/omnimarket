# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage tests for node_capsule_effectiveness_feedback_reducer.

Pins the node's contract-declared publish topics to their literal declared
values, so a silent contract rename or removal fails here instead of at a live
runtime boundary.
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
    / "node_capsule_effectiveness_feedback_reducer"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_capsule_effectiveness_feedback_declares_output_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.context-roi-score-completed.v1" in publish_topics
    assert "onex.evt.omnimarket.capsule-effectiveness-hypothesis.v1" in publish_topics
