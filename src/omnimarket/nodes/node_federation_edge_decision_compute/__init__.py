# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Federation edge decision compute: decide one recorded runtime event at the edge.

``HandlerFederationEdgeDecision.handle(ModelEdgeDecisionRequest) ->
ModelEdgeDecision``. Pure: the decision selects the recording's event on the
overlay's allowed topic, checks the connection's who-and-where, the recorded
ACL probe and the egress gate, and answers the record as it may leave the
wire or one named refusal.
"""

from omnimarket.nodes.node_federation_edge_decision_compute.handlers.handler_federation_edge_decision import (
    HandlerFederationEdgeDecision,
    decide_edge,
)


class NodeFederationEdgeDecisionCompute(HandlerFederationEdgeDecision):
    """ONEX entry-point wrapper for HandlerFederationEdgeDecision."""


__all__ = [
    "HandlerFederationEdgeDecision",
    "NodeFederationEdgeDecisionCompute",
    "decide_edge",
]
