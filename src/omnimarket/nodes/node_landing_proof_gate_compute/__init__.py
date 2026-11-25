# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_proof_gate_compute — the landing proof gate: PROVEN or DRAFT, with the reasons named."""

from omnimarket.nodes.node_landing_proof_gate_compute.handlers.handler_landing_proof_gate import (
    HandlerLandingProofGate,
)


class NodeLandingProofGateCompute(HandlerLandingProofGate):
    """ONEX entry-point wrapper for HandlerLandingProofGate."""


__all__ = ["HandlerLandingProofGate", "NodeLandingProofGateCompute"]
