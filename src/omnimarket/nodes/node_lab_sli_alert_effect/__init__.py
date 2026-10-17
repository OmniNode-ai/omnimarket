# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_sli_alert_effect: lab SLI breaches turned into actions (OMN-17427)."""

from omnimarket.nodes.node_lab_sli_alert_effect.handlers.handler_lab_sli_alert import (
    HandlerLabSliAlert,
)

__all__ = ["HandlerLabSliAlert", "NodeLabSliAlertEffect"]


class NodeLabSliAlertEffect(HandlerLabSliAlert):
    """ONEX entry-point wrapper for HandlerLabSliAlert."""
