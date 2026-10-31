# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Policy-driven release action classification."""

from __future__ import annotations

from omnimarket.nodes.node_release_action_classify_compute.handlers.handler_release_action_classify import (
    HandlerReleaseActionClassify,
)

__all__ = ["HandlerReleaseActionClassify", "NodeReleaseActionClassifyCompute"]


class NodeReleaseActionClassifyCompute(HandlerReleaseActionClassify):
    """ONEX entry-point wrapper."""
