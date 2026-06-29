# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node entry point for baselines quality snapshot projection."""

from __future__ import annotations

from omnimarket.nodes.node_projection_baselines_quality.handlers.handler_projection_baselines_quality import (
    HandlerProjectionBaselinesQuality,
)


class NodeProjectionBaselinesQuality(HandlerProjectionBaselinesQuality):
    """ONEX entry-point wrapper for HandlerProjectionBaselinesQuality."""


__all__ = ["NodeProjectionBaselinesQuality"]
