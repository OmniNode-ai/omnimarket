# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_reading_compute: render seam readings as verdict lines (OMN-21459)."""

from omnimarket.nodes.node_seam_reading_compute.handlers.handler_seam_reading import (
    HandlerSeamReading,
)

__all__ = ["HandlerSeamReading", "NodeSeamReadingCompute"]


class NodeSeamReadingCompute(HandlerSeamReading):
    """ONEX entry-point wrapper for HandlerSeamReading."""
