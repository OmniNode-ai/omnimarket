# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_work_ledger_reducer_scheduled_effect: the work-ledger reducer on the runtime tick (OMN-24305)."""

from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.handlers.handler_work_ledger_reducer_scheduled import (
    HandlerWorkLedgerReducerScheduled,
)

__all__ = ["HandlerWorkLedgerReducerScheduled", "NodeWorkLedgerReducerScheduledEffect"]


class NodeWorkLedgerReducerScheduledEffect(HandlerWorkLedgerReducerScheduled):
    """ONEX entry-point wrapper for HandlerWorkLedgerReducerScheduled."""
