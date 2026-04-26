# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_overseer_verifier — deterministic verification layer for overseer model outputs."""

from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)


class NodeOverseerVerifier:
    """ONEX entry-point marker for node_overseer_verifier."""

    __onex_node_type__ = "node_overseer_verifier"


__all__ = ["ModelVerifierRequest", "NodeOverseerVerifier"]
