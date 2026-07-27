# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC attestation-observe node (OMN-14393, report-only attestation gate)."""

from omnimarket.nodes.node_occ_attestation_observe.handlers.handler_occ_attestation_observe import (
    HandlerOccAttestationObserve,
)
from omnimarket.nodes.node_occ_attestation_observe.models.model_occ_attestation_observe_request import (
    ModelOccAttestationObserveRequest,
)


class NodeOccAttestationObserve(HandlerOccAttestationObserve):
    """ONEX entry-point wrapper for HandlerOccAttestationObserve (OMN-14393)."""


__all__ = [
    "HandlerOccAttestationObserve",
    "ModelOccAttestationObserveRequest",
    "NodeOccAttestationObserve",
]
