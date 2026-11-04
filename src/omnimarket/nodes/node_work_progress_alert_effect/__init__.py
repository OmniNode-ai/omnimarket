# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Heartbeat-carried stall alert effect: lanes past their phase budget reach Slack."""

from __future__ import annotations

from omnimarket.nodes.node_work_progress_alert_effect.handlers.handler_work_progress_alert import (
    HandlerWorkProgressAlert,
)

__all__ = ["HandlerWorkProgressAlert", "NodeWorkProgressAlertEffect"]


class NodeWorkProgressAlertEffect(HandlerWorkProgressAlert):
    """ONEX entry-point wrapper for HandlerWorkProgressAlert."""
