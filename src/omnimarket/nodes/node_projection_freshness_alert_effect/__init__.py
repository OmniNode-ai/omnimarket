# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Heartbeat-carried projection freshness alert effect (OMN-19513)."""

from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.handler_projection_freshness_alert import (
    HandlerProjectionFreshnessAlert,
)

__all__ = ["HandlerProjectionFreshnessAlert", "NodeProjectionFreshnessAlertEffect"]


class NodeProjectionFreshnessAlertEffect(HandlerProjectionFreshnessAlert):
    """ONEX entry-point wrapper for HandlerProjectionFreshnessAlert."""
