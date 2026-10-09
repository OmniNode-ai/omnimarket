# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handshake policy gate decision compute node (OMN-20671)."""

from omnimarket.nodes.node_handshake_policy_gate_compute.handlers.handler_handshake_policy_gate import (
    HandlerHandshakePolicyGate,
)


class NodeHandshakePolicyGateCompute(HandlerHandshakePolicyGate):
    """ONEX entrypoint for the handshake policy gate's deterministic decisions."""


__all__ = ["HandlerHandshakePolicyGate", "NodeHandshakePolicyGateCompute"]
