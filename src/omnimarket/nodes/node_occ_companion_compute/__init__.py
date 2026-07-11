# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_attestation import (
    HandlerOccCompanionAttestation,
)
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    HandlerOccCompanionCompute,
)


class NodeOccCompanionCompute(HandlerOccCompanionCompute):
    """ONEX entry-point wrapper for HandlerOccCompanionCompute (RSD-1, OMN-14285)."""


__all__ = [
    "HandlerOccCompanionAttestation",
    "HandlerOccCompanionCompute",
    "NodeOccCompanionCompute",
]
