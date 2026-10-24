# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_merge_sweep_same_head_compute — pure compute policy (OMN-20864)."""

from omnimarket.nodes.node_merge_sweep_same_head_compute.handlers.handler_merge_sweep_same_head import (
    HandlerMergeSweepSameHead,
)


class NodeMergeSweepSameHeadCompute(HandlerMergeSweepSameHead):
    """ONEX entry-point wrapper for HandlerMergeSweepSameHead."""


__all__ = ["HandlerMergeSweepSameHead", "NodeMergeSweepSameHeadCompute"]
