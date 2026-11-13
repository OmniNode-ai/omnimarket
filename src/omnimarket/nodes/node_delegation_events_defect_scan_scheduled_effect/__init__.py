# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_events_defect_scan_scheduled_effect: the delegation_events defect scan on the runtime tick (OMN-24271)."""

from omnimarket.nodes.node_delegation_events_defect_scan_scheduled_effect.handlers.handler_defect_scan_scheduled import (
    HandlerDefectScanScheduled,
)

__all__ = [
    "HandlerDefectScanScheduled",
    "NodeDelegationEventsDefectScanScheduledEffect",
]


class NodeDelegationEventsDefectScanScheduledEffect(HandlerDefectScanScheduled):
    """ONEX entry-point wrapper for HandlerDefectScanScheduled."""
