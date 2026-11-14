# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lease receipt reducer: one lease command over one kind's table, as a pure reduce.

``HandlerLeaseReceiptReducer.handle(ModelLeaseReduceInput) ->
ModelLeaseReduceOutput``. The caller owns the table: it passes one kind's
table in and writes the recorded reply's back; every refusal carries the
input table unchanged and a typed reason. No clock, no I/O, no state file.
"""

from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
    reduce_lease,
)


class NodeLeaseReceiptReducer(HandlerLeaseReceiptReducer):
    """ONEX entry-point wrapper for HandlerLeaseReceiptReducer."""


__all__ = [
    "HandlerLeaseReceiptReducer",
    "NodeLeaseReceiptReducer",
    "reduce_lease",
]
