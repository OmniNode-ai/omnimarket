# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_conservation_compute: reconcile the two sides of one seam for one finalized window."""

from omnimarket.nodes.node_seam_conservation_compute.handlers.handler_seam_conservation import (
    HandlerSeamConservation,
)

__all__ = ["HandlerSeamConservation", "NodeSeamConservationCompute"]


class NodeSeamConservationCompute(HandlerSeamConservation):
    """ONEX entry-point wrapper for HandlerSeamConservation."""
