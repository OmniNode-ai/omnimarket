# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DoD closeout sweep decision compute node (OMN-20675)."""

from omnimarket.nodes.node_dod_closeout_decision_compute.handlers.handler_dod_closeout_decision import (
    HandlerDodCloseoutDecision,
)


class NodeDodCloseoutDecisionCompute(HandlerDodCloseoutDecision):
    """ONEX entrypoint for the closer's deterministic decisions."""


__all__ = ["HandlerDodCloseoutDecision", "NodeDodCloseoutDecisionCompute"]
