# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node entry point for context-ROI scores projection."""

from __future__ import annotations

from omnimarket.nodes.node_projection_context_roi.handlers.handler_projection_context_roi import (
    HandlerProjectionContextRoi,
)


class NodeProjectionContextRoi(HandlerProjectionContextRoi):
    """ONEX entry-point wrapper for HandlerProjectionContextRoi."""


__all__ = ["NodeProjectionContextRoi"]
