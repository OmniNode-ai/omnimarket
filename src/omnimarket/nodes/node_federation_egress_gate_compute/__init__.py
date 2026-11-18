# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Federation egress gate compute: pass or refuse one record crossing buses.

``HandlerFederationEgressGate.handle(ModelEgressGateRequest) ->
ModelEgressGateResult``. Pure: the gate checks the record's shape against the
topic policy carried in the request, binds the key to the value it names, and
carries the key only as its sha256 hex digest, the value only as its size.
"""

from omnimarket.nodes.node_federation_egress_gate_compute.handlers.handler_federation_egress_gate import (
    HandlerFederationEgressGate,
    gate_egress,
)


class NodeFederationEgressGateCompute(HandlerFederationEgressGate):
    """ONEX entry-point wrapper for HandlerFederationEgressGate."""


__all__ = [
    "HandlerFederationEgressGate",
    "NodeFederationEgressGateCompute",
    "gate_egress",
]
