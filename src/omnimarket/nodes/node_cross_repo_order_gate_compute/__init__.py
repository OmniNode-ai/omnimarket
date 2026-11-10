# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_cross_repo_order_gate_compute: the declared merge order of a change split across repositories (OMN-23345)."""

from omnimarket.nodes.node_cross_repo_order_gate_compute.handlers.handler_cross_repo_order_gate import (
    HandlerCrossRepoOrderGate,
)


class NodeCrossRepoOrderGateCompute(HandlerCrossRepoOrderGate):
    """ONEX entry-point wrapper for HandlerCrossRepoOrderGate."""


__all__ = ["HandlerCrossRepoOrderGate", "NodeCrossRepoOrderGateCompute"]
