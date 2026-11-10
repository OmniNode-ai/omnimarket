# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_state_receipts_feeder_scheduled_effect: gathers the lane receipts where the projection reads its receipts on the runtime's schedule (OMN-23427)."""

from omnimarket.nodes.node_work_state_receipts_feeder_scheduled_effect.handlers.handler_receipts_feeder_scheduled import (
    HandlerWorkStateReceiptsFeederScheduled,
)

__all__ = [
    "HandlerWorkStateReceiptsFeederScheduled",
    "NodeWorkStateReceiptsFeederScheduledEffect",
]


class NodeWorkStateReceiptsFeederScheduledEffect(
    HandlerWorkStateReceiptsFeederScheduled
):
    """ONEX entry-point wrapper for HandlerWorkStateReceiptsFeederScheduled."""
