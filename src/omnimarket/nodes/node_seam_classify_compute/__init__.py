# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_classify_compute: operation-routed seam classification (OMN-21459)."""

from omnimarket.nodes.node_seam_classify_compute.handlers.handler_seam_classify import (
    HandlerSeamClassify,
)

__all__ = ["HandlerSeamClassify", "NodeSeamClassifyCompute"]


class NodeSeamClassifyCompute(HandlerSeamClassify):
    """ONEX entry-point wrapper for HandlerSeamClassify."""
