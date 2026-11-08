# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-class andon thresholds, frozen with a hash, and the burst scope decision (piece 7)."""

from .handlers.handler_threshold_compute import HandlerThresholdCompute


class NodeAndonThresholdCompute(HandlerThresholdCompute):
    """Registered ONEX node."""
