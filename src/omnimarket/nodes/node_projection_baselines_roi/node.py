# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node entry point for baselines ROI snapshot projection."""

from __future__ import annotations

from omnimarket.nodes.node_projection_baselines_roi.handlers.handler_projection_baselines_roi import (
    HandlerProjectionBaselinesRoi,
)


class NodeProjectionBaselinesRoi(HandlerProjectionBaselinesRoi):
    """ONEX entry-point wrapper for HandlerProjectionBaselinesRoi."""


__all__ = ["NodeProjectionBaselinesRoi"]
