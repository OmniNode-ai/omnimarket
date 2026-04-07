# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build loop state reducer - FSM with circuit breaker."""

from __future__ import annotations

from omnibase_core.nodes.node_reducer import NodeReducer


class NodeLoopStateReducer(NodeReducer):
    """Declarative reducer for build loop FSM state tracking.

    All behavior is defined in contract.yaml - no custom logic here.
    """

    # Pure declarative shell - all behavior defined in contract.yaml


__all__ = ["NodeLoopStateReducer"]
