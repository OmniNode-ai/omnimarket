# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ONEX entrypoint for the gate-accept node, bound to its handler by contract."""

from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_gate_accept import (
    HandlerGateAccept,
)


class NodeAgentReviewGateAcceptEffect(HandlerGateAccept):
    """Effect: one gate attempt in, one declared decision out; ports are injected."""


__all__ = ["NodeAgentReviewGateAcceptEffect"]
