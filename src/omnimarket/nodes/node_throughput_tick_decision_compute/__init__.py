# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Throughput tick decision compute node (OMN-20686)."""

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)


class NodeThroughputTickDecisionCompute(HandlerThroughputTickDecision):
    """ONEX entrypoint for the merge-throughput tick's deterministic decisions."""


__all__ = ["HandlerThroughputTickDecision", "NodeThroughputTickDecisionCompute"]
