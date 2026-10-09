# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hourly tick decision compute node (OMN-20680)."""

from omnimarket.nodes.node_hourly_tick_decision_compute.handlers.handler_hourly_tick_decision import (
    HandlerHourlyTickDecision,
)


class NodeHourlyTickDecisionCompute(HandlerHourlyTickDecision):
    """ONEX entrypoint for the tick's deterministic decisions."""


__all__ = ["HandlerHourlyTickDecision", "NodeHourlyTickDecisionCompute"]
