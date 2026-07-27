# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared state-coverage regression test for node_dispatch_queue_drainer.

OMN-13501 (faked-boundary burn-down) surfaced this node under the AST-hardened
state-coverage gate (OMN-13816) because the burn-down touched the node's tests,
which promotes its previously-baselined (grandfathered) declared output states
to a hard strict-mode requirement. Following the OMN-13674 declared-state
coverage convention, this test pins the node's contract-declared publish
topic(s) — the wire strings the runtime auto-emits — to their literal values so
a silent contract rename or removal fails here instead of only at a live
runtime/projection boundary.
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
    / "node_dispatch_queue_drainer"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT_PATH.read_text())


def test_dispatch_queue_drainer_declares_output_topics() -> None:
    """Every contract-declared publish topic keeps its literal wire string."""
    publish_topics = _load_contract()["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.dispatch-queue-drain-completed.v1" in publish_topics
    assert "onex.evt.omnimarket.dispatch-queue-drain-failed.v1" in publish_topics
