# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handshake policy gate run effect node (OMN-20671): reads GitHub, asks the compute node, reports."""

from omnimarket.nodes.node_handshake_policy_gate_effect.handlers.handler_handshake_policy_gate_run import (
    HandlerHandshakePolicyGateRun,
)


class NodeHandshakePolicyGateEffect(HandlerHandshakePolicyGateRun):
    """ONEX entrypoint for the handshake policy gate run."""


__all__ = ["HandlerHandshakePolicyGateRun", "NodeHandshakePolicyGateEffect"]
